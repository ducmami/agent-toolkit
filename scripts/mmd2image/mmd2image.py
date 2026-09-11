#!/usr/bin/env python3
"""Convert Mermaid diagrams to SVG/PNG via @mermaid-js/mermaid-cli (mmdc).

Supports:
- .mmd / .mermaid: whole file is one diagram
- .md / .markdown / .mdx: extract ```mermaid fences

Output defaults to <input-dir>/images/<stem>/imageN.svg (or .png with --fileType png)
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

MARKDOWN_EXTENSIONS = {".md", ".markdown", ".mdx"}
MERMAID_EXTENSIONS = {".mmd", ".mermaid"}

# Opening fence: ```mermaid with optional info-string after; closing ```
MERMAID_FENCE_RE = re.compile(
    r"^```mermaid(?:[ \t][^\n]*)?\n(.*?)^```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)


def extract_diagrams(path: Path) -> list[str]:
    """Return non-empty Mermaid source strings from the input file."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in MERMAID_EXTENSIONS:
        body = text.strip()
        if not body:
            print(f"WARNING: empty Mermaid file: {path}", file=sys.stderr)
            return []
        return [body]

    if suffix in MARKDOWN_EXTENSIONS:
        diagrams: list[str] = []
        for match in MERMAID_FENCE_RE.finditer(text):
            body = match.group(1).strip()
            if not body:
                print(
                    f"WARNING: skipping empty mermaid fence in {path}",
                    file=sys.stderr,
                )
                continue
            diagrams.append(body)
        return diagrams

    raise ValueError(
        f"Unsupported extension '{path.suffix}'. "
        f"Use one of: {', '.join(sorted(MARKDOWN_EXTENSIONS | MERMAID_EXTENSIONS))}"
    )


INSTALL_HINT = (
    "Install @mermaid-js/mermaid-cli, then retry:\n"
    "  npm install -g @mermaid-js/mermaid-cli\n"
    "or locally:\n"
    f"  cd {SCRIPT_DIR}\n"
    "  npm install"
)


def resolve_mmdc_cmd() -> list[str]:
    """Prefer global/PATH mmdc; fall back to local npx in SCRIPT_DIR."""
    for name in ("mmdc.cmd", "mmdc"):
        found = shutil.which(name)
        if found:
            return [found]

    for name in ("npx.cmd", "npx"):
        npx = shutil.which(name)
        if npx:
            return [npx, "--no-install", "mmdc"]

    raise FileNotFoundError(
        "mmdc/npx not found on PATH. Install Node.js, then:\n" + INSTALL_HINT
    )


def run_mmdc(
    mmdc_cmd: list[str],
    input_mmd: Path,
    output_path: Path,
    background: str,
) -> None:
    cmd = [
        *mmdc_cmd,
        "-i",
        str(input_mmd),
        "-o",
        str(output_path),
        "-b",
        background,
        "-q",
    ]
    # Local npx --no-install needs cwd with node_modules; global mmdc ignores this.
    try:
        result = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            "Failed to run mmdc. " + INSTALL_HINT
        ) from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        hint = ""
        combined = f"{stderr}\n{stdout}".lower()
        if "could not determine executable" in combined or "not found" in combined:
            hint = f"\nHint: {INSTALL_HINT}"
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise RuntimeError(f"mmdc failed: {detail}{hint}")


def convert_file(
    input_path: Path,
    out_dir: Path | None,
    background: str,
    file_type: str = "svg",
) -> list[Path]:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    diagrams = extract_diagrams(input_path)
    if not diagrams:
        raise RuntimeError(f"No Mermaid diagrams found in {input_path}")

    target_dir = out_dir.resolve() if out_dir is not None else (
        input_path.parent / "images" / input_path.stem
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    mmdc_cmd = resolve_mmdc_cmd()
    written: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="mmd2image-") as tmp:
        tmp_dir = Path(tmp)
        for index, source in enumerate(diagrams, start=1):
            temp_mmd = tmp_dir / f"diagram-{index}.mmd"
            temp_mmd.write_text(source + "\n", encoding="utf-8")
            out_path = target_dir / f"image{index}.{file_type}"
            try:
                run_mmdc(mmdc_cmd, temp_mmd, out_path, background)
            except RuntimeError as exc:
                raise RuntimeError(
                    f"Failed converting diagram {index}/{len(diagrams)} "
                    f"from {input_path}: {exc}"
                ) from exc
            if not out_path.is_file() or out_path.stat().st_size == 0:
                raise RuntimeError(
                    f"mmdc reported success but output missing or empty: {out_path}"
                )
            written.append(out_path)

    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Mermaid (.mmd/.mermaid) or Markdown mermaid fences to SVG/PNG "
            "using @mermaid-js/mermaid-cli."
        ),
    )
    parser.add_argument(
        "input",
        type=Path,
        help="Input .md / .markdown / .mdx / .mmd / .mermaid file.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="Output directory. Defaults to <input-dir>/images/<stem>.",
    )
    parser.add_argument(
        "--fileType",
        dest="file_type",
        choices=("svg", "png"),
        default="svg",
        help="Output format (mmdc -o extension). Default: svg.",
    )
    parser.add_argument(
        "--background",
        default="transparent",
        help="Background color for SVG/PNG (mmdc -b). Default: transparent.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        paths = convert_file(
            args.input.resolve(),
            args.out_dir,
            args.background,
            args.file_type,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for path in paths:
        print(path)
    label = args.file_type.upper()
    print(f"Wrote {len(paths)} {label} file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
