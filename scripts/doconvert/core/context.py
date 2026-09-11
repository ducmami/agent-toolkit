"""Shared runtime context for conversion functions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Context:
    script_dir: Path
    repo_root: Path
    verbose: bool = False
