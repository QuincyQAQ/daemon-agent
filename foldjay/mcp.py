"""MCP 2025-06-18 stdio client; no HTTP transport or server-initiated sampling."""
from __future__ import annotations

import json
import os
import queue
import re
import select
import signal
import subprocess
import threading
import time
from functools import partial

from .tools import Tool


class MCPClient:
    def __init__(self, command, cwd, env=None, timeout=20):
        if not isinstance(command, list) or not command or not all(isinstance(x,str) for x in command):
            raise ValueError("MCP command must be an argv list")
        child_env = {k: v for k,v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL", "TMPDIR"}}
        child_env.update(env or {})
        self.process = subprocess.Popen(command, cwd=cwd, env=child_env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        start_new_session=True)
        self.timeout, self.next_id = timeout, 0
        os.set_blocking(self.process.stdin.fileno(), False)
        self.inbox = queue.Queue(maxsize=128)
        self.closed = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        try:
            info = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "daemon-agent", "version": "0.1.0"}})
            if info.get("protocolVersion") not in {"2025-06-18", "2024-11-05"}:
                raise ValueError("Unsupported MCP protocol version")
            self.notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            while not self.closed:
                raw = self.process.stdout.readline(2_000_001)
                if not raw:
                    raise EOFError("MCP server closed stdout")
                if len(raw) > 2_000_000:
                    raise ValueError("MCP message exceeds limit")
                self.inbox.put(json.loads(raw), timeout=1)
        except Exception as exc:
            try:
                self.inbox.put(exc, timeout=1)
            except queue.Full:
                pass

    def send(self, message):
        data = memoryview((json.dumps(message)+"\n").encode())
        if len(data) > 2_000_000:
            raise ValueError("MCP request exceeds limit")
        fd = self.process.stdin.fileno()
        deadline = time.monotonic() + self.timeout
        while data:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                self.close()
                raise TimeoutError("MCP write timed out")
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                continue

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params):
        self.next_id += 1
        rid = self.next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.inbox.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                self.close()
                raise TimeoutError("MCP request timed out: " + method) from None
            if isinstance(message, Exception):
                self.close()
                raise message
            if "method" in message:
                if "id" in message:
                    response = {"result": {}} if message["method"] == "ping" else {"error": {"code": -32601, "message": "Unsupported server request"}}
                    self.send({"jsonrpc": "2.0", "id": message["id"], **response})
                continue
            if message.get("id") != rid:
                continue
            if "error" in message:
                raise RuntimeError("MCP: " + str(message["error"]))
            return message.get("result", {})

    def discover(self):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            page = self.request("tools/list", {"cursor": cursor} if cursor else {})
            result.extend(page.get("tools", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError("MCP repeated pagination cursor")
            seen.add(cursor)
        raise ValueError("Too many MCP tool pages")

    def register(self, registry, server):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,20}", server):
            raise ValueError("Invalid MCP server name")
        for item in self.discover():
            original = item["name"]
            name = "mcp_"+server+"_"+re.sub(r"[^a-zA-Z0-9_-]", "_", original)
            if len(name) > 64:
                raise ValueError("MCP tool name too long")
            registry.add(Tool(name, item.get("description", original), item.get("inputSchema", {"type": "object"}),
                              partial(self.call_tool, original), "external"))

    def call_tool(self, name, /, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            result["error"] = "MCP tool reported an error"
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.process.wait()
        self.process.stdout.close()
        self.thread.join(timeout=1)
