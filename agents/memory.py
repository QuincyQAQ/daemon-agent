import time
import uuid
from .retrieval import rank


class Memory:
    def __init__(self, store):
        self.store = store

    def add(self, text, kind="fact"):
        if kind not in {"fact", "preference"} or not text.strip() or len(text) > 4000:
            raise ValueError("Memory needs a fact/preference, 1–4000 characters")
        mid = uuid.uuid4().hex[:12]
        with self.store.db:
            self.store.db.execute("INSERT INTO memories VALUES(?,?,?,?)", (mid, kind, text, time.time()))
        self.store.audit("memory.add", {"id": mid, "kind": kind})
        return mid

    def search(self, query, limit=5):
        rows = {r["id"]: dict(r) for r in self.store.db.execute("SELECT * FROM memories")}
        return [rows[key] for key, _ in rank(query, [(k, r["text"]) for k,r in rows.items()], limit)]

    def delete(self, mid):
        with self.store.db:
            self.store.db.execute("DELETE FROM memories WHERE id=?", (mid,))
        self.store.audit("memory.delete", {"id": mid})
