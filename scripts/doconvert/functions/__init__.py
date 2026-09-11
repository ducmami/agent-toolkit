"""Registry of conversion functions."""

from __future__ import annotations

from core.base import ConvertFunction

from .confluence import ConfluenceFunction
from .markdown import MarkdownFunction
from .mermaid import MermaidFunction

FUNCTIONS: list[ConvertFunction] = [
    ConfluenceFunction(),
    MermaidFunction(),
    MarkdownFunction(),
]

__all__ = ["FUNCTIONS", "ConfluenceFunction", "MermaidFunction", "MarkdownFunction"]
