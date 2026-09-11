"""Stub: generic Markdown conversion (HTML/PDF — not yet implemented)."""

from __future__ import annotations

import argparse
from pathlib import Path

from core.base import ConvertFunction
from core.context import Context


class MarkdownFunction(ConvertFunction):
    name = "markdown"
    help = (
        "Convert Markdown to HTML or PDF "
        "(not implemented yet - use confluence for Confluence storage HTML)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "input",
            type=Path,
            help="Input Markdown file.",
        )
        parser.add_argument(
            "output",
            type=Path,
            help="Output file path.",
        )
        parser.add_argument(
            "--to",
            choices=("html", "pdf"),
            default="html",
            help="Target format (default: html).",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        raise NotImplementedError(
            "doconvert markdown is not implemented yet. "
            "For Confluence body.storage use: doconvert confluence INPUT.md OUTPUT.html"
        )
