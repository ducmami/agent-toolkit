#!/usr/bin/env python3
"""Document conversion CLI.

Usage::

    python doconvert.py --help
    python doconvert.py confluence INPUT.md OUTPUT.html
    python doconvert.py mermaid --help
    python doconvert.py markdown --help
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from core.context import Context
from functions import FUNCTIONS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doconvert",
        description=(
            "Convert documents between formats. "
            "Subcommands: confluence (GFM to Confluence body.storage), "
            "mermaid (stub), markdown (stub)."
        ),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print extra progress to stderr.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for func in FUNCTIONS:
        cmd = sub.add_parser(func.name, help=func.help)
        func.add_arguments(cmd)
        cmd.set_defaults(_func=func)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = Context(
        script_dir=SCRIPT_DIR,
        repo_root=SCRIPT_DIR.parent.parent,
        verbose=args.verbose,
    )
    try:
        return args._func.run(args, ctx)
    except NotImplementedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
