from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False)


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".foldjay-", dir=path.parent)
    try:
        if path.exists():
            os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        database = root / "foldjay.sqlite3"
        legacy = root / "bear.sqlite3"
        if legacy.is_file() and not database.exists():
            database = legacy
        self.db = sqlite3.connect(database, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, kind TEXT, text TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY, title TEXT, body TEXT,
          version INTEGER, status TEXT, retrieved INTEGER DEFAULT 0, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS history(skill_id TEXT, version INTEGER, body TEXT, created REAL,
          PRIMARY KEY(skill_id, version));
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, kind TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS replays(id TEXT PRIMARY KEY, skill_id TEXT, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS evaluations(id TEXT PRIMARY KEY, skill_id TEXT, version INTEGER,
          score REAL, payload TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS champions(skill_id TEXT PRIMARY KEY, version INTEGER, evaluation_id TEXT);
        """)

    def close(self):
        self.db.close()

    def audit(self, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO audit(kind,payload,created) VALUES(?,?,?)",
                            (kind, dumps(payload), time.time()))

    def session_path(self, sid):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", sid):
            raise ValueError("Invalid session id")
        return self.root / "sessions" / (sid + ".json")

    @contextlib.contextmanager
    def session_lock(self, sid):
        path = self.session_path(sid).with_suffix(".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("This session is already running") from None
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self, session):
        session["updated"] = time.time()
        atomic_write(self.session_path(session["id"]), dumps(session))

    def load(self, sid, workspace):
        session = json.loads(self.session_path(sid).read_text())
        if session["workspace"] != str(workspace):
            raise ValueError("Session belongs to a different workspace")
        # A crash between execution and checkpoint cannot prove whether a mutation happened.
        # Supply unknown results; never automatically repeat an interrupted call.
        calls = {}
        for message in session["messages"]:
            for call in message.get("tool_calls", []):
                calls[call["id"]] = call
            if message["role"] == "tool":
                calls.pop(message["tool_call_id"], None)
        for cid in calls:
            session["messages"].append({"role": "tool", "tool_call_id": cid,
                "content": dumps({"error": "Interrupted: outcome unknown. Inspect state before retrying."})})
        if calls:
            self.audit("recovery", {"session": sid, "unknown_calls": list(calls)})
        from .context import recover_tool_evidence
        if recover_tool_evidence(session, self):
            self.audit("recovery.evidence", {"session": sid})
        return session

    def new(self, workspace, parent=None):
        return {"id": uuid.uuid4().hex, "workspace": str(workspace), "parent": parent,
                "messages": [], "state": {}, "pending": None, "created": time.time()}
