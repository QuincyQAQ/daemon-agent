from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    workspace: Path
    state: Path
    provider: str = "demo"
    model: str = ""
    mode: str = "act"
    max_steps: int = 24
    max_depth: int = 2
    context_chars: int = 128000
    timeout: float = 60
    evolution: bool = True
    api_base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    direct_connection: bool = False
    max_output_tokens: int | None = None
    model_timeout: float | None = None
    model_retries: int = 2
    thinking: str | None = None

    def __post_init__(self):
        self.workspace = self.workspace.expanduser().resolve()
        self.state = self.state.expanduser().resolve()
        if self.mode not in {"act", "plan"}:
            raise ValueError("mode must be act or plan")
        if self.max_steps < 1 or self.context_chars < 4000 or self.timeout <= 0:
            raise ValueError("Invalid runtime limits")
        if self.model_timeout is not None and self.model_timeout <= 0:
            raise ValueError("Model timeout must be positive")
        if self.model_retries < 0 or self.thinking not in (None, "enabled", "disabled"):
            raise ValueError("Invalid model request options")
        if not self.workspace.is_dir():
            raise ValueError("Workspace must be an existing directory")

    @classmethod
    def from_env(cls, workspace=".", state=None, **kwargs):
        root = Path(workspace).resolve()
        return cls(root, Path(state or root / ".bear"),
                   provider=os.getenv("BEAR_PROVIDER", "demo"),
                   model=os.getenv("BEAR_MODEL", ""), **kwargs)
