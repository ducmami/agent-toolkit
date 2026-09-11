"""Stub: Mermaid diagram conversion (local PNG/SVG via mmdc — not yet implemented)."""

from __future__ import annotations

import argparse
from pathlib import Path

from core.base import ConvertFunction
from core.context import Context


class MermaidFunction(ConvertFunction):
    name = "mermaid"
    help = (
        "Convert Mermaid diagrams (.mmd / mermaid fences in .md) to PNG or SVG "
        "(not implemented yet - use scripts/mmd2image meanwhile)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "input",
            type=Path,
            help="Input .mmd / .mermaid / .md file.",
        )
        parser.add_argument(
            "output",
            type=Path,
            help="Output file or directory for rendered diagram(s).",
        )
        parser.add_argument(
            "--fileType",
            dest="file_type",
            choices=("png", "svg"),
            default="svg",
            help="Output format (default: svg).",
        )
        parser.add_argument(
            "--background",
            default="transparent",
            help="Background color (default: transparent).",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        raise NotImplementedError(
            "doconvert mermaid is not implemented yet. "
            "Use: python scripts/mmd2image/mmd2image.py <input> --fileType png|svg"
        )
