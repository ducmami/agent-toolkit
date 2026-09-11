"""Convert GitHub Flavored Markdown to Confluence body.storage XHTML."""

from __future__ import annotations

import argparse
import html
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import markdown

from core.base import ConvertFunction
from core.context import Context
from utils.confluence_storage import html_to_storage
from utils.helpers import (
    ensure_markdown_input,
    read_text,
    strip_frontmatter,
    warn,
    write_text,
)
from utils.mermaid_ink import URL_WARN_LENGTH, mermaid_ink_png_url


def _make_mermaid_formatter(
    theme: str,
    bg_color: str | None,
) -> Callable[..., str]:
    def formatter(
        source: str,
        language: str,  # noqa: ARG001
        css_class: str,  # noqa: ARG001
        options: dict[str, Any],
        md: Any,  # noqa: ARG001
        **kwargs: Any,  # noqa: ARG001
    ) -> str:
        body = source.strip()
        if not body:
            warn("Skipping empty mermaid fence")
            return ""

        use_theme = (options or {}).get("theme") or theme
        use_bg = (options or {}).get("bg_color") or bg_color
        url = mermaid_ink_png_url(body, theme=use_theme, bg_color=use_bg)
        if len(url) > URL_WARN_LENGTH:
            warn(
                f"Mermaid.ink URL is very long ({len(url)} chars); "
                "proxies or Confluence may reject it."
            )
        safe_url = url.replace('"', "&quot;")
        escaped = html.escape(body, quote=False)
        return (
            '<div class="doconvert-mermaid">'
            f'<img alt="mermaid diagram" src="{safe_url}" />'
            f'<pre><code class="language-mermaid">{escaped}</code></pre>'
            "</div>"
        )

    return formatter


def _build_markdown(
    *,
    mermaid_theme: str = "default",
    mermaid_bg: str | None = None,
) -> markdown.Markdown:
    extensions = [
        "tables",
        "sane_lists",
        "toc",
        "pymdownx.superfences",
        "pymdownx.highlight",
        "pymdownx.tasklist",
        "pymdownx.tilde",
        "pymdownx.magiclink",
        "pymdownx.betterem",
        "pymdownx.quotes",
        "pymdownx.blocks.admonition",
    ]
    extension_configs = {
        "pymdownx.highlight": {
            "use_pygments": False,
            "guess_lang": False,
        },
        "pymdownx.tasklist": {
            "custom_checkbox": True,
            "clickable_checkbox": False,
        },
        "pymdownx.superfences": {
            "custom_fences": [
                {
                    "name": "mermaid",
                    "class": "mermaid",
                    "format": _make_mermaid_formatter(mermaid_theme, mermaid_bg),
                },
            ],
        },
        "pymdownx.quotes": {
            "callouts": True,
        },
        "pymdownx.blocks.admonition": {
            "types": ["note", "tip", "important", "warning", "caution"],
        },
    }
    return markdown.Markdown(extensions=extensions, extension_configs=extension_configs)


def convert_markdown_to_storage(
    text: str,
    *,
    image_base_url: str | None = None,
    toc: bool = False,
    mermaid_theme: str = "default",
    mermaid_bg: str | None = None,
    no_code_macro: bool = False,
) -> str:
    """Convert GFM markdown text to Confluence ``body.storage`` XHTML fragment."""
    body = strip_frontmatter(text)
    md = _build_markdown(mermaid_theme=mermaid_theme, mermaid_bg=mermaid_bg)
    html = md.convert(body)
    return html_to_storage(
        html,
        image_base_url=image_base_url,
        toc=toc,
        no_code_macro=no_code_macro,
    )


class ConfluenceFunction(ConvertFunction):
    name = "confluence"
    help = (
        "Convert GitHub Flavored Markdown to Confluence body.storage XHTML. "
        "Normal fences become code macros; mermaid fences become mermaid.ink PNG "
        "images plus an expand macro with the source."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "input",
            type=Path,
            help="Input Markdown file (.md / .markdown / .mdx).",
        )
        parser.add_argument(
            "output",
            type=Path,
            help="Output file for Confluence storage XHTML fragment.",
        )
        parser.add_argument(
            "--toc",
            action="store_true",
            help="Insert a Confluence TOC macro at the start of the body.",
        )
        parser.add_argument(
            "--image-base-url",
            default=None,
            help="Base URL prepended to relative <img> src (not used for mermaid.ink).",
        )
        parser.add_argument(
            "--mermaid-theme",
            choices=("default", "neutral", "dark", "forest"),
            default="default",
            help="Mermaid theme embedded in mermaid.ink state (default: default).",
        )
        parser.add_argument(
            "--mermaid-bg",
            default=None,
            metavar="COLOR",
            help=(
                "Optional mermaid.ink bgColor (e.g. !white or FFFFFF). "
                "Omit for transparent background."
            ),
        )
        parser.add_argument(
            "--no-code-macro",
            action="store_true",
            help="Keep <pre><code> for normal fences instead of Confluence code macros.",
        )

    def run(self, args: argparse.Namespace, ctx: Context) -> int:
        input_path = ensure_markdown_input(args.input)
        output_path = args.output.resolve()
        text = read_text(input_path)
        storage = convert_markdown_to_storage(
            text,
            image_base_url=args.image_base_url,
            toc=args.toc,
            mermaid_theme=args.mermaid_theme,
            mermaid_bg=args.mermaid_bg,
            no_code_macro=args.no_code_macro,
        )
        write_text(output_path, storage)
        if ctx.verbose:
            print(f"Wrote {len(storage)} chars to {output_path}", file=sys.stderr)
        else:
            print(output_path)
        return 0
