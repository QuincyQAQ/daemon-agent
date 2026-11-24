from __future__ import annotations

from pathlib import Path


class Denied(PermissionError):
    pass


class Policy:
    """Application-level guardrails. Arbitrary processes require a real OS sandbox."""
    def __init__(self, workspace, state, mode="act", approve=None):
        self.workspace = Path(workspace).resolve()
        self.state = Path(state).resolve()
        self.mode = mode
        self.approve = approve or (lambda action, detail: False)

    def path(self, value):
        path = (self.workspace / value).resolve()
        if not path.is_relative_to(self.workspace):
            raise Denied("Path is outside workspace")
        if path == self.state or path.is_relative_to(self.state):
            raise Denied("Runtime state must be accessed through dedicated tools")
        if any(part in {".git", ".ssh", ".aws", ".codex", ".agents", ".bear"} or
               part == ".env" or part.startswith(".env.") and part != ".env.example" or
               part.endswith((".pem", ".key")) for part in path.relative_to(self.workspace).parts):
            raise Denied("Protected path")
        return path

    def check(self, action, detail, effect="read"):
        if self.mode == "plan" and effect != "read":
            raise Denied("Plan Mode permits read-only tools only")
        if effect in {"write", "execute", "external"} and not self.approve(action, detail):
            raise Denied("Approval denied: " + action)
