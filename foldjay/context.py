from __future__ import annotations

import json
import re
import uuid
from .providers import DemoProvider, json_completion
from .storage import atomic_write, dumps

EVIDENCE_VERSION = 2


def fit_json_text(text, budget, suffix=""):
    """Fit a string inside a JSON field budget, counting escapes but not the enclosing quotes."""
    if len(dumps(text)) - 2 <= budget:
        return text
    if budget < len(suffix):
        return ""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(dumps(text[:middle])) - 2 + len(suffix) <= budget:
            low = middle
        else:
            high = middle - 1
    return text[:low] + suffix


def retrieved_context(memories, skills, max_chars):
    data = {"memory": memories, "skills": skills}
    encoded = dumps(data)
    if len(encoded) <= max_chars:
        return encoded
    # Preserve every retrieved ID, even when the original rows contain long bodies.
    data = {"memory": [{"id": m["id"], "kind": m["kind"], "text": ""} for m in memories],
            "skills": [{"id": s["id"], "title": "", "body": ""} for s in skills]}
    fields = [(row, "text", m["text"]) for row, m in zip(data["memory"], memories)]
    for row, skill in zip(data["skills"], skills):
        fields.extend([(row, "title", skill["title"]), (row, "body", skill["body"])])
    # Fit shorter fields first, then share the remaining space among longer fields.
    fields.sort(key=lambda field: len(dumps(field[2])))
    remaining = max_chars - len(dumps(data))
    for index, (row, key, value) in enumerate(fields):
        row[key] = fit_json_text(value, remaining // (len(fields) - index), suffix="…")
        remaining -= len(dumps(row[key])) - 2
    return dumps(data)


def tool_payload(name, result):
    payload = dumps(result)
    if len(payload) <= 18000:
        return payload
    if name == "read_file" and isinstance(result.get("content"), str):
        # Keep a real contiguous prefix so the next offset and historical evidence are reliable.
        content = result["content"]
        visible = dict(result, content="", truncated=True, observed_chars=len(content),
                       observation_truncated=result["truncated"])
        visible["content"] = fit_json_text(content, 18000 - len(dumps(visible)))
        return dumps(visible)
    return dumps({"truncated": True, "preview": payload[:16000], "error": result.get("error")})


def tool_evidence(messages, previous=None):
    """Keep bounded, deterministic observations independently of model summaries."""
    records = {}
    for entry in (previous or {}).get("records", []):
        if isinstance(entry, dict):
            records[(entry.get("tool"), entry.get("path"), entry.get("offset", 0))] = dict(entry)
    calls = {}
    for message in messages:
        for call in message.get("tool_calls", []):
            calls[call.get("id")] = call.get("function", {})
        if message.get("role") != "tool":
            continue
        function = calls.get(message.get("tool_call_id"), {})
        name = function.get("name")
        if name not in {"list_files", "read_file", "write_file", "edit_file"}:
            continue
        try:
            args = json.loads(function.get("arguments", "{}"))
            result = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict) or not isinstance(result, dict) or result.get("error") is not None:
            continue
        entry = {"tool": name, "path": str(args.get("path", "."))[:500], "status": "observed_success"}
        if name == "list_files" and isinstance(result.get("files"), list):
            files = [f[:300] for f in result["files"] if isinstance(f, str)]
            entry.update(files=files[:80], listing_truncated=bool(result.get("truncated")) or len(files) > 80)
        elif name == "read_file" and isinstance(result.get("content"), str):
            content = result["content"]
            entry.update(offset=args.get("offset", 0), returned_chars=len(content),
                         total_chars=result.get("total_chars"), read_truncated=bool(result.get("truncated")),
                         sha256=result.get("sha256"), excerpt=content[:600],
                         excerpt_truncated=len(content) > 600,
                         outline="\n".join(line[:200] for line in content.splitlines()
                             if re.match(r"^\s*(?:class |(?:async )?def |#{1,6} )", line))[:1000])
            if "observed_chars" in result and "observation_truncated" in result:
                entry.update(observed_chars=result["observed_chars"],
                             observation_truncated=result["observation_truncated"])
        elif name == "read_file" and result.get("truncated") and isinstance(result.get("preview"), str) and "error" in result:
            # Older oversized successes only saved an incomplete JSON preview.
            entry.update(offset=args.get("offset", 0), payload_truncated=True)
        elif name in {"write_file", "edit_file"} and result.get("written"):
            entry.update(sha256=result.get("sha256"))
        else:
            continue
        records[(name, entry["path"], entry.get("offset", 0))] = entry
    entries = list(records.values())
    omitted = (previous or {}).get("omitted_records", 0) + max(0, len(entries)-80)
    entries = entries[-80:]
    # Retain paths/status even when excerpts must be dropped to bound prompt size.
    if len(dumps(entries)) > 28000:
        for entry in entries:
            if "excerpt" in entry:
                entry["excerpt"] = entry["excerpt"][:150]
                entry["excerpt_truncated"] = True
            if "outline" in entry:
                entry["outline"] = entry["outline"][:400]
            if "files" in entry and len(entry["files"]) > 20:
                entry["files"] = entry["files"][:20]
                entry["listing_truncated"] = True
    while entries and len(dumps(entries)) > 28000:
        entries.pop(0)
        omitted += 1
    return {"version": EVIDENCE_VERSION, "records": entries, "omitted_records": omitted,
            "scope": "Historical tool observations; excerpts and outlines are partial. Missing records do not prove an action never happened."}


def recover_tool_evidence(session, store):
    """Migrate older compacted sessions using only this session's local archives."""
    state = session.get("state", {})
    existing = state.get("tool_evidence", {})
    if not state.get("archive") or existing.get("version", 0) >= EVIDENCE_VERSION:
        return False
    evidence = {}
    archives = sorted((store.root / "sessions" / "archives").glob(session["id"] + "-*.json"),
                      key=lambda path: path.stat().st_mtime)
    for archive in archives:
        # Never follow an arbitrary archive path supplied by a model or symlink.
        if archive.is_symlink():
            continue
        try:
            messages = json.loads(archive.read_text())
            if isinstance(messages, list):
                evidence = tool_evidence(messages, evidence)
        except (OSError, ValueError):
            continue
    # Preserve the last saved observations even if older archives are no longer present.
    evidence["records"] = evidence.get("records", []) + existing.get("records", [])
    evidence["omitted_records"] = max(evidence.get("omitted_records", 0), existing.get("omitted_records", 0))
    state["tool_evidence"] = tool_evidence(session["messages"], evidence)
    session["state"] = state
    return True


def recent_tool_batches(messages, max_chars):
    """Retain full recent observations, deduplicating reads without orphaned tool IDs."""
    batches = []
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        results = {}
        for result in messages[index+1:]:
            if result.get("role") != "tool":
                break
            results[result.get("tool_call_id")] = result
        if all(call.get("id") in results for call in message["tool_calls"]):
            batches.append((message, results))
    retained, seen, size = [], set(), 0
    for message, results in reversed(batches):
        calls, outputs = [], []
        for call in reversed(message["tool_calls"]):
            result = results[call["id"]]
            key = None
            try:
                args = json.loads(call["function"]["arguments"])
                payload = json.loads(result["content"])
                # A reuse receipt points into the pre-fold transcript. Only retain
                # the original observation if it fits the new transcript.
                if call["function"]["name"] == "read_file" and payload.get("already_in_context") is True:
                    continue
                if call["function"]["name"] == "read_file" and isinstance(payload.get("content"), str):
                    # Newest observation of the same range wins, even if file contents changed.
                    key = (str(args.get("path")), args.get("offset", 0), args.get("limit", 12000))
            except (ValueError, TypeError, AttributeError):
                pass
            if key is not None and key in seen:
                continue
            if key is not None:
                seen.add(key)
            cost = len(dumps([call, result])) + 100
            if size + cost > max_chars:
                continue
            size += cost
            calls.insert(0, call)
            outputs.insert(0, result)
        if calls:
            retained.insert(0, [{"role": "assistant", "content": "", "tool_calls": calls}] + outputs)
    return [message for batch in retained for message in batch]


def is_read_receipt(content):
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("already_in_context") is True


def compact(session, store, provider, reason="manual", max_chars=128000):
    messages = session["messages"]
    if len(messages) < 3:
        return {"compacted": False}
    archive = store.root / "sessions" / "archives" / (session["id"] + "-" + uuid.uuid4().hex[:8] + ".json")
    atomic_write(archive, dumps(messages))
    users = [m["content"] for m in messages if m["role"] == "user"]
    previous = session.get("state", {})
    evidence = tool_evidence(messages, previous.get("tool_evidence"))
    episodes = list(previous.get("episodic", []))
    episodes += [m.get("content", "")[:500] for m in messages if m["role"] == "assistant" and not m.get("tool_calls")]
    tool_memory = [entry for entry in previous.get("tool_memory", []) if not is_read_receipt(entry)]
    tool_memory += [m["content"][:800] for m in messages if m["role"] == "tool"
                    and not is_read_receipt(m["content"])]
    state = {"goal": session.get("task") or (users[-1][:3000] if users else previous.get("goal", "")),
             "episodic": episodes[-6:], "working_memory": previous.get("working_memory", ""),
             "tool_memory": tool_memory[-8:], "reason": reason, "archive": str(archive),
             "tool_evidence": evidence}
    # Automatic size management needs no extra, potentially slow model request.
    if reason != "context" and not isinstance(provider, DemoProvider):
        try:
            summary = json_completion(provider,
                "Fold this agent session into structured working state. Preserve exact paths, outcomes, unresolved "
                "errors, user constraints and next steps. Do not claim unfinished work is complete. "
                "Use tool_evidence to cover earlier observations too, not only the recent messages. "
                "An omitted excerpt does not mean the file was never read. "
                "Return {goal:string,episodic:array of strings,working_memory:string,tool_memory:array of strings}.",
                {"previous": previous, "tool_evidence": evidence,
                 "recent": [{**m, "content": str(m.get("content", ""))[:2000]} for m in messages[-16:]]})
            if (isinstance(summary.get("goal"), str) and isinstance(summary.get("working_memory"), str)
                    and all(isinstance(summary.get(k), list) and all(isinstance(x,str) for x in summary[k]) for k in ("episodic", "tool_memory"))
                    and len(dumps(summary)) <= 14000):
                state.update({key: summary[key] for key in ("goal", "episodic", "working_memory", "tool_memory")})
        except Exception as exc:
            store.audit("compact.fallback", {"type": type(exc).__name__})
    # Preserve complete tool call/result pairs within a bound, not all-or-nothing batches.
    user = {"role": "user", "content": "Continue the current task from the saved working state: " + state["goal"]}
    retained_budget = max(0, int(max_chars * 0.75) - len(dumps(state)) - len(dumps(user)) - 4000)
    tail = recent_tool_batches(messages, retained_budget)
    if messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls"):
        tail += [{"role": "assistant", "content": messages[-1].get("content", "")[:3000]}]
    session["state"] = state
    session["messages"] = [user] + tail
    store.audit("compact", {"session": session["id"], "reason": reason, "archive": str(archive)})
    store.save(session)
    return {"compacted": True, "archive": str(archive),
            "retained_tool_results": sum(m["role"] == "tool" for m in tail)}
