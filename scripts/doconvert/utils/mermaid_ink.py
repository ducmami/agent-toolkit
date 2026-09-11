"""Encode Mermaid source as mermaid.ink / mermaid.live pako URLs."""

from __future__ import annotations

import base64
import json
import zlib
from urllib.parse import urlencode

MERMAID_INK_IMG = "https://mermaid.ink/img"
MERMAID_THEMES = ("default", "neutral", "dark", "forest")

# Soft warning threshold for very long image URLs (proxies / browsers)
URL_WARN_LENGTH = 8000


def pako_encode(state: dict) -> str:
    """Serialize state as ``pako:<base64url>`` (mermaid.live compatible)."""
    payload = json.dumps(state, separators=(",", ":")).encode("utf-8")
    compressed = zlib.compress(payload, 9)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return f"pako:{encoded}"


def mermaid_ink_png_url(
    source: str,
    *,
    theme: str = "default",
    bg_color: str | None = None,
) -> str:
    """Build a PNG image URL for a Mermaid diagram via mermaid.ink.

    ``/img`` defaults to JPEG; ``type=png`` is required for PNG.
    """
    if theme not in MERMAID_THEMES:
        raise ValueError(
            f"Invalid mermaid theme {theme!r}. "
            f"Choose one of: {', '.join(MERMAID_THEMES)}"
        )
    state = {"code": source, "mermaid": {"theme": theme}}
    serialized = pako_encode(state)
    query: dict[str, str] = {"type": "png"}
    if bg_color:
        query["bgColor"] = bg_color
    return f"{MERMAID_INK_IMG}/{serialized}?{urlencode(query)}"


def decode_pako_state(pako_token: str) -> dict:
    """Decode ``pako:...`` back to the JSON state (for tests / round-trip)."""
    token = pako_token
    if token.startswith("pako:"):
        token = token[5:]
    padding = "=" * ((4 - len(token) % 4) % 4)
    raw = base64.urlsafe_b64decode(token + padding)
    return json.loads(zlib.decompress(raw).decode("utf-8"))
