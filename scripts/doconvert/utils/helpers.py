"""File helpers, frontmatter strip, and language name mapping."""

from __future__ import annotations

import re
import sys
from pathlib import Path

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}

# Confluence code macro language aliases
LANGUAGE_MAP = {
    "js": "javascript",
    "ts": "typescript",
    "sh": "bash",
    "shell": "bash",
    "yml": "yaml",
    "py": "python",
    "rb": "ruby",
    "cs": "csharp",
    "c#": "csharp",
    "plaintext": "text",
    "plain": "text",
}

# YAML frontmatter: --- ... --- at start of file
FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n.*?\r?\n---[ \t]*\r?\n?",
    re.DOTALL,
)


def strip_frontmatter(text: str) -> str:
    """Remove leading YAML frontmatter if present."""
    body = text.lstrip("\ufeff")
    match = FRONTMATTER_RE.match(body)
    if match:
        return body[match.end() :]
    return body


def read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def ensure_markdown_input(path: Path) -> Path:
    resolved = path.resolve()
    if resolved.suffix.lower() not in MARKDOWN_EXTENSIONS:
        raise ValueError(
            f"Unsupported extension '{path.suffix}'. "
            f"Use one of: {', '.join(sorted(MARKDOWN_EXTENSIONS))}"
        )
    return resolved


def map_language(lang: str | None) -> str | None:
    if not lang:
        return None
    key = lang.strip().lower()
    if not key:
        return None
    return LANGUAGE_MAP.get(key, key)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)
