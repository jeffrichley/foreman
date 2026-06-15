"""V4Config — TOML-loaded daemon configuration.

This IS a pydantic model: config is a boundary surface. TOML on disk
is untrusted input the daemon parses at startup; pydantic's validation
catches typos, wrong types, invalid enum values before the daemon
half-starts and confuses everything downstream.

Schema:
  [daemon]
    db_path              - SQLite DB path
    log_dir              - directory for rich-stdout + jsonl
    log_level            - default INFO
    tick_seconds         - cadence between Poller ticks (default 30)
    max_in_flight        - Single concurrency knob: sizes BOTH the QM
                           in-flight cap AND the WorkerPool
                           ThreadPoolExecutor. One number so a stuck-
                           ticket scenario can't strand pool threads
                           while the QM still has slots (or vice
                           versa). Default 1 (serial); opt in to higher
                           after dogfood stability.
    role_timeout_seconds - Subprocess timeout per role invocation
                           (default 600 = 10 min). Phase 7.5 threads
                           this to SubprocessRoleDispatcher.
    merge_mechanism      - "queue" (default) | "merge" | "squash" | "rebase"

  [[projects]]
    name              - project slug
    repo              - "owner/name"
    local_clone_path  - filesystem path
    trigger_label     - GH label (default "foreman:plan")
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    repo: str
    local_clone_path: str
    trigger_label: str = "foreman:plan"


class V4Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    db_path: str
    log_dir: str
    log_level: str = "INFO"
    tick_seconds: float = 30.0
    max_in_flight: int = 1
    role_timeout_seconds: int = 600
    merge_mechanism: Literal["queue", "merge", "squash", "rebase"] = "queue"
    projects: list[ProjectConfig] = Field(default_factory=list)


def load_config(path: Path) -> V4Config:
    """Parse the TOML at ``path`` and validate as V4Config.

    Invalid TOML / missing required fields / wrong types raise pydantic
    ValidationError. The daemon's startup catches these and exits with
    a useful message; this function deliberately does not swallow.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    daemon = raw.get("daemon", {})
    projects = raw.get("projects", [])
    return V4Config(**daemon, projects=projects)
