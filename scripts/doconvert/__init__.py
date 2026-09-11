"""Document conversion CLI / library (Confluence body.storage, etc.)."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from functions.confluence import convert_markdown_to_storage

__all__ = ["convert_markdown_to_storage"]
