from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

from .morning import morning_report
from .permissions import Denied
from .storage import atomic_write


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def schema(properties=None, required=None):
    return {"type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}


STRING = {"type": "string"}
SEARCH_FILE_LIMIT = 5000


def validate(value, spec, path="arguments"):
    """Validate the JSON Schema subset used by built-ins; MCP servers validate their schemas."""
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    kind = spec.get("type")
    if kind in types and (not isinstance(value, types[kind]) or kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(path + " has invalid type")
    if "enum" in spec and value not in spec["enum"]:
        raise ValueError(path + " has invalid value")
    if isinstance(value, dict):
        properties = spec.get("properties", {})
        if any(k not in value for k in spec.get("required", [])):
            raise ValueError(path + " is missing required fields")
        if spec.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(path + " contains unknown fields")
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key], path + "." + key)
    if isinstance(value, list) and "items" in spec:
        for item in value:
            validate(item, spec["items"], path + "[]")


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: object
    effect: str = "read"

    def spec(self):
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                   "parameters": self.parameters}}


class Tools:
    def __init__(self, policy, store, timeout=30):
        self.policy, self.store, self.timeout = policy, store, min(timeout, 120)
        self.items = {}
        self.gpu_snapshot = None
        self.add(Tool("gpu_inventory", "查询 Slurm 全部 GPU 或空闲候选。Slurm IDX is node-local. "
                      "Use free_only for free GPUs; follow next_offset and snapshot for the complete list.",
                      schema({"free_only": {"type": "boolean"}, "model": STRING, "partition": STRING,
                              "offset": {"type": "integer"}, "snapshot": STRING}), self.gpu_inventory))
        self.add(Tool("morning_report", "Verify two experiment groups against experiment.json and return paired scores and evidence.",
                      schema({"path": STRING}, ["path"]), self.morning_report))
        self.add(Tool("list_files", "List workspace files, excluding protected paths.",
                      schema({"path": STRING}), self.list_files))
        self.add(Tool("read_file", "Read UTF-8 text and its sha256. Large files require offset/limit.",
                      schema({"path": STRING, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]), self.read_file))
        self.add(Tool("search", "Find literal text in up to 5000 workspace files. Returns up to 50 matching lines; "
                      "truncated means more matches or unsearched files remain.",
                      schema({"query": STRING, "path": STRING}, ["query"]), self.search))
        self.add(Tool("write_file", "Create/replace UTF-8 file. Existing files REQUIRE the sha256 from read_file; new files use NEW.",
                      schema({"path": STRING, "content": STRING, "expected_hash": STRING}, ["path", "content", "expected_hash"]), self.write_file, "write"))
        self.add(Tool("edit_file", "Replace exactly one occurrence. Requires current sha256 and unique nonempty old text.",
                      schema({"path": STRING, "old": STRING, "new": STRING, "expected_hash": STRING}, ["path", "old", "new", "expected_hash"]), self.edit_file, "write"))
        self.add(Tool("shell", "Execute a shell command after explicit approval. Every command may mutate or access the host.",
                      schema({"command": STRING}, ["command"]), self.shell, "execute"))

    def gpu_inventory(self, free_only=False, model=None, partition=None, offset=0, snapshot=None):
        from .gpus import inventory, select
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        if snapshot is not None:
            if not self.gpu_snapshot or self.gpu_snapshot["observed_at"] != snapshot:
                raise ValueError("GPU snapshot expired. Query again from offset 0.")
        elif offset:
            raise ValueError("Pass the snapshot timestamp when requesting another page")
        else:
            self.gpu_snapshot = inventory()
        report = select(self.gpu_snapshot, free_only=free_only, model=model, partition=partition)
        rows = report["rows"]
        report.update(rows=rows[offset:offset+12], row_count=len(rows),
                      next_offset=offset+12 if offset+12 < len(rows) else None)
        return report

    def morning_report(self, path):
        return morning_report(self.policy, path)

    def add(self, tool):
        if tool.name in self.items:
            raise ValueError("Duplicate tool: " + tool.name)
        self.items[tool.name] = tool

    def specs(self):
        return [t.spec() for t in self.items.values() if self.policy.mode != "plan" or t.effect == "read"]

    def call(self, name, arguments):
        started = time.monotonic()
        try:
            tool = self.items.get(name)
            if tool is None:
                raise ValueError("Unknown tool: " + name)
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            # MCP servers validate arguments against their own full JSON Schema.
            if tool.effect != "external":
                validate(arguments, tool.parameters)
            self.policy.check(name, arguments, tool.effect)
            result = tool.handler(**arguments)
            result = result if isinstance(result, dict) else {"result": result}
        except (Exception,) as exc:
            result = {"error": str(exc), "type": type(exc).__name__}
        self.store.audit("tool", {"name": name, "ok": "error" not in result,
                                  "duration": round(time.monotonic()-started, 4)})
        return result

    def walk(self, path=".", limit=SEARCH_FILE_LIMIT):
        root = self.policy.path(path)
        if root.is_file():
            yield root
            return
        count = 0
        for directory, dirs, files in os.walk(root, followlinks=False):
            kept = []
            for name in sorted(dirs):
                if name in {"node_modules", "__pycache__", ".venv", "pic"}:
                    continue
                try:
                    child = self.policy.path(str(os.path.join(directory, name)))
                    if not os.path.islink(os.path.join(directory, name)):
                        kept.append(name)
                except Denied:
                    pass
            dirs[:] = kept
            for name in sorted(files):
                try:
                    file = self.policy.path(str(os.path.join(directory, name)))
                except Denied:
                    continue
                if file.is_file():
                    yield file
                    count += 1
                    if count >= limit:
                        return

    def list_files(self, path="."):
        files = []
        for file in self.walk(path):
            if len(files) == 300:
                return {"files": files, "truncated": True}
            files.append(str(file.relative_to(self.policy.workspace)))
        return {"files": files, "truncated": False}

    def _text(self, path):
        if path.stat().st_size > 2_000_000:
            raise ValueError("File exceeds 2 MB text limit")
        with path.open(encoding="utf-8", newline="") as stream:
            return stream.read()

    def read_file(self, path, offset=0, limit=12000):
        if offset < 0 or not 1 <= limit <= 16000:
            raise ValueError("offset >= 0; limit between 1 and 16000")
        text = self._text(self.policy.path(path))
        return {"path": path, "sha256": sha256(text), "content": text[offset:offset+limit],
                "total_chars": len(text), "truncated": offset+limit < len(text)}

    def search(self, query, path="."):
        if not query or len(query) > 500:
            raise ValueError("Search query must contain 1–500 characters")
        matches = []
        for index, file in enumerate(self.walk(path, limit=SEARCH_FILE_LIMIT + 1)):
            if index == SEARCH_FILE_LIMIT:
                return {"matches": matches, "truncated": True}
            try:
                text = self._text(file)
            except (OSError, UnicodeError, ValueError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    if len(matches) == 50:
                        return {"matches": matches, "truncated": True}
                    matches.append({"path": str(file.relative_to(self.policy.workspace)), "line": number, "text": line[:500]})
        return {"matches": matches, "truncated": False}

    def _check_hash(self, path, expected_hash):
        if path.exists():
            if sha256(self._text(path)) != expected_hash:
                raise ValueError("Stale edit: read the file again before modifying")
        elif expected_hash != "NEW":
            raise ValueError("New file requires expected_hash=NEW")

    def write_file(self, path, content, expected_hash):
        file = self.policy.path(path)
        if len(content.encode()) > 2_000_000:
            raise ValueError("Content exceeds limit")
        self._check_hash(file, expected_hash)
        atomic_write(file, content)
        return {"path": path, "sha256": sha256(content), "written": True}

    def edit_file(self, path, old, new, expected_hash):
        file = self.policy.path(path)
        self._check_hash(file, expected_hash)
        text = self._text(file)
        if not old or text.count(old) != 1:
            raise ValueError("Old text must be nonempty and match exactly once")
        return self.write_file(path, text.replace(old, new, 1), expected_hash)

    def shell(self, command):
        if not command.strip() or len(command) > 16000:
            raise ValueError("Invalid shell command")
        env = {k: v for k,v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TERM", "TMPDIR"}}
        # Output goes to a temporary file to avoid unbounded capture in RAM. Kill at 2 MB.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace", newline="") as output:
            process = subprocess.Popen(["/bin/sh", "-c", command], cwd=self.policy.workspace,
                                       env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline, reason = time.monotonic()+self.timeout, None
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline or os.fstat(output.fileno()).st_size > 2_000_000:
                        reason = "timeout or output limit"
                        break
                    time.sleep(0.03)
            finally:
                # Also clean up descendants left behind by a shell that exited early.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            output.seek(0)
            data = output.read(16001)
            result = {"exit_code": process.returncode, "output": data[:16000],
                      "truncated": len(data) > 16000}
            if reason or process.returncode:
                result["error"] = reason or "command failed"
            return result
