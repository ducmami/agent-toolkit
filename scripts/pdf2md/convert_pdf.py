#!/usr/bin/env python3
"""Convert one PDF into a Markdown draft plus extracted visual assets.

The script is intentionally conservative: it extracts text and links directly
from the PDF, saves embedded images, renders likely vector diagrams as crops,
and inserts D2 placeholders for later AI-assisted redrawing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - exercised by users without deps
    print(
        "Missing dependency: PyMuPDF. Install it with:\n"
        "  python -m pip install -r scripts/pdf2md/requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)


MIN_IMAGE_AREA_RATIO = 0.01
MIN_IMAGE_WIDTH_PT = 80
MIN_IMAGE_HEIGHT_PT = 40
MIN_DIAGRAM_AREA_RATIO = 0.06
MIN_DIAGRAM_WIDTH_PT = 160
MIN_DIAGRAM_HEIGHT_PT = 90
DIAGRAM_CLUSTER_MARGIN_PT = 12
RENDER_ZOOM = 2.0
URL_RE = re.compile(r"https?://[^\s)>\]]+")


@dataclass(frozen=True)
class LinkInfo:
    page: int
    bbox: tuple[float, float, float, float]
    target: str
    kind: str
    text: str


@dataclass(frozen=True)
class AssetInfo:
    id: str
    asset_type: str
    page: int
    bbox: tuple[float, float, float, float]
    path: Path
    relative_path: str
    width: float
    height: float
    source: str
    near_text: str


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value).strip("-").lower()
    return slug or "document"


def parse_page_range(page_range: str | None, page_count: int) -> list[int]:
    if not page_range:
        return list(range(page_count))

    pages: set[int] = set()
    for part in page_range.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_s, end_s = token.split("-", 1)
            start = int(start_s)
            end = int(end_s)
            if start > end:
                raise ValueError(f"Invalid descending page range: {token}")
            pages.update(range(start - 1, end))
        else:
            pages.add(int(token) - 1)

    invalid = [page + 1 for page in pages if page < 0 or page >= page_count]
    if invalid:
        raise ValueError(f"Page(s) out of range: {invalid}")
    return sorted(pages)


def rect_to_tuple(rect: fitz.Rect) -> tuple[float, float, float, float]:
    return (
        round(float(rect.x0), 2),
        round(float(rect.y0), 2),
        round(float(rect.x1), 2),
        round(float(rect.y1), 2),
    )


def rect_area(rect: fitz.Rect) -> float:
    if rect.is_empty or rect.is_infinite:
        return 0.0
    return max(0.0, rect.width) * max(0.0, rect.height)


def relative_to(path: Path, base: Path) -> str:
    try:
        rel = path.resolve().relative_to(base.resolve())
    except ValueError:
        rel = path.resolve()
    return rel.as_posix()


def ensure_output_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Input is not a PDF: {pdf_path}")

    out_path = args.out.resolve() if args.out else pdf_path.with_suffix(".md")
    manifest_path = out_path.with_suffix(".assets.json")

    if args.asset_dir:
        asset_dir = args.asset_dir.resolve()
    else:
        asset_dir = pdf_path.parent / "assets" / slugify(pdf_path.stem)

    if not args.overwrite:
        conflicts = [p for p in (out_path, manifest_path) if p.exists()]
        if asset_dir.exists() and any(asset_dir.iterdir()):
            conflicts.append(asset_dir)
        if conflicts:
            joined = "\n  ".join(str(p) for p in conflicts)
            raise FileExistsError(
                "Refusing to overwrite existing output. Use --overwrite for:\n"
                f"  {joined}"
            )

    asset_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    return out_path, manifest_path, asset_dir


def get_line_text(line: dict[str, Any]) -> str:
    text = "".join(span.get("text", "") for span in line.get("spans", []))
    return normalize_text(text)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u00a0", " ")).strip()


def median_font_size(blocks: Iterable[dict[str, Any]]) -> float:
    sizes: list[float] = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = normalize_text(span.get("text", ""))
                if text:
                    sizes.append(float(span.get("size", 0)))
    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def line_max_font_size(line: dict[str, Any]) -> float:
    sizes = [
        float(span.get("size", 0))
        for span in line.get("spans", [])
        if normalize_text(span.get("text", ""))
    ]
    return max(sizes) if sizes else 0.0


def line_bbox(line: dict[str, Any]) -> fitz.Rect:
    return fitz.Rect(line.get("bbox", (0, 0, 0, 0)))


def block_bbox(block: dict[str, Any]) -> fitz.Rect:
    return fitz.Rect(block.get("bbox", (0, 0, 0, 0)))


def sort_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(blocks, key=lambda b: (round(float(b.get("bbox", [0, 0])[1]) / 4), b.get("bbox", [0])[0]))


def markdown_escape_link_text(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def link_target(link: dict[str, Any]) -> tuple[str, str]:
    kind = int(link.get("kind", 0))
    if kind == fitz.LINK_URI and link.get("uri"):
        return str(link["uri"]), "uri"
    if kind in (fitz.LINK_GOTO, fitz.LINK_GOTOR):
        page = link.get("page")
        if isinstance(page, int) and page >= 0:
            return f"#page-{page + 1}", "internal"
    if kind == fitz.LINK_NAMED and link.get("name"):
        return f"named:{link['name']}", "named"
    return "unknown", f"kind-{kind}"


def extract_links(page: fitz.Page, page_number: int) -> list[LinkInfo]:
    words = page.get_text("words")
    results: list[LinkInfo] = []
    seen: set[tuple[str, tuple[float, float, float, float]]] = set()
    for link in page.get_links():
        rect = fitz.Rect(link.get("from", (0, 0, 0, 0)))
        if rect.is_empty:
            continue
        target, kind = link_target(link)
        linked_words = [
            str(word[4])
            for word in words
            if rect.intersects(fitz.Rect(word[:4]))
        ]
        text = normalize_text(" ".join(linked_words))
        key = (target, rect_to_tuple(rect))
        if key in seen:
            continue
        seen.add(key)
        results.append(
            LinkInfo(
                page=page_number,
                bbox=rect_to_tuple(rect),
                target=target,
                kind=kind,
                text=text,
            )
        )
    for word in words:
        text = str(word[4])
        for match in URL_RE.finditer(text):
            target = match.group(0).rstrip(".,;:")
            rect = fitz.Rect(word[:4])
            key = (target, rect_to_tuple(rect))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                LinkInfo(
                    page=page_number,
                    bbox=rect_to_tuple(rect),
                    target=target,
                    kind="text-uri",
                    text=target,
                )
            )
    return results


def links_for_rect(links: list[LinkInfo], rect: fitz.Rect) -> list[LinkInfo]:
    return [link for link in links if fitz.Rect(link.bbox).intersects(rect)]


def format_line_as_markdown(
    line: dict[str, Any],
    body_size: float,
    page_links: list[LinkInfo],
) -> str:
    text = get_line_text(line)
    if not text:
        return ""

    linked = links_for_rect(page_links, line_bbox(line))
    if len(linked) == 1 and linked[0].target not in ("unknown", ""):
        if linked[0].text and linked[0].text in text:
            text = text.replace(
                linked[0].text,
                f"[{markdown_escape_link_text(linked[0].text)}]({linked[0].target})",
                1,
            )
        else:
            text = f"[{markdown_escape_link_text(text)}]({linked[0].target})"
    elif linked:
        refs = ", ".join(
            f"[{i + 1}]({link.target})" for i, link in enumerate(linked) if link.target != "unknown"
        )
        if refs:
            text = f"{text} {refs}"

    max_size = line_max_font_size(line)
    is_short = len(text) <= 120
    if max_size >= body_size * 1.65 and is_short:
        return f"### {text}"
    if max_size >= body_size * 1.35 and is_short:
        return f"#### {text}"

    return text


def text_near_rect(page: fitz.Page, rect: fitz.Rect, padding: float = 45) -> str:
    near = fitz.Rect(rect)
    near.x0 -= padding
    near.y0 -= padding
    near.x1 += padding
    near.y1 += padding
    snippets: list[str] = []
    for word in page.get_text("words"):
        word_rect = fitz.Rect(word[:4])
        if near.intersects(word_rect):
            snippets.append(str(word[4]))
    return normalize_text(" ".join(snippets))[:500]


def pixmap_from_xref(doc: fitz.Document, xref: int) -> fitz.Pixmap:
    pix = fitz.Pixmap(doc, xref)
    if pix.n >= 5:
        pix = fitz.Pixmap(fitz.csRGB, pix)
    return pix


def save_embedded_images(
    doc: fitz.Document,
    page: fitz.Page,
    page_index: int,
    asset_dir: Path,
    md_base: Path,
) -> list[AssetInfo]:
    page_rect = page.rect
    page_area = rect_area(page_rect)
    assets: list[AssetInfo] = []
    counter = 0
    seen: set[tuple[int, tuple[float, float, float, float]]] = set()

    for image in page.get_images(full=True):
        xref = int(image[0])
        rects = page.get_image_rects(xref)
        if not rects:
            rects = [page_rect]
        for rect in rects:
            if rect.width < MIN_IMAGE_WIDTH_PT or rect.height < MIN_IMAGE_HEIGHT_PT:
                continue
            if page_area and rect_area(rect) / page_area < MIN_IMAGE_AREA_RATIO:
                continue
            key = (xref, rect_to_tuple(rect))
            if key in seen:
                continue
            seen.add(key)
            counter += 1
            asset_id = f"page-{page_index + 1:03d}-image-{counter:02d}"
            path = asset_dir / f"{asset_id}.png"
            pix = pixmap_from_xref(doc, xref)
            try:
                pix.save(path)
            finally:
                pix = None
            assets.append(
                AssetInfo(
                    id=asset_id,
                    asset_type="image",
                    page=page_index + 1,
                    bbox=rect_to_tuple(rect),
                    path=path,
                    relative_path=relative_to(path, md_base),
                    width=round(float(rect.width), 2),
                    height=round(float(rect.height), 2),
                    source=f"xref:{xref}",
                    near_text=text_near_rect(page, rect),
                )
            )
    return assets


def drawing_rects(page: fitz.Page) -> list[fitz.Rect]:
    rects: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect", (0, 0, 0, 0)))
        if rect.is_empty or rect.is_infinite:
            continue
        if rect.width < 4 or rect.height < 4:
            continue
        rects.append(rect)
    return rects


def expanded(rect: fitz.Rect, margin: float, clip: fitz.Rect) -> fitz.Rect:
    value = fitz.Rect(rect.x0 - margin, rect.y0 - margin, rect.x1 + margin, rect.y1 + margin)
    return value & clip


def cluster_rects(rects: list[fitz.Rect], page_rect: fitz.Rect) -> list[fitz.Rect]:
    clusters = [expanded(rect, DIAGRAM_CLUSTER_MARGIN_PT, page_rect) for rect in rects]
    changed = True
    while changed:
        changed = False
        merged: list[fitz.Rect] = []
        while clusters:
            current = clusters.pop(0)
            i = 0
            while i < len(clusters):
                if current.intersects(clusters[i]) or current.contains(clusters[i]) or clusters[i].contains(current):
                    current |= clusters.pop(i)
                    changed = True
                else:
                    i += 1
            merged.append(current & page_rect)
        clusters = merged
    return clusters


def overlaps_existing_asset(rect: fitz.Rect, assets: list[AssetInfo]) -> bool:
    rect_area_value = rect_area(rect)
    if rect_area_value == 0:
        return False
    for asset in assets:
        other = fitz.Rect(asset.bbox)
        intersection = rect & other
        if rect_area(intersection) / min(rect_area_value, rect_area(other) or rect_area_value) > 0.85:
            return True
    return False


def render_diagram_crops(
    page: fitz.Page,
    page_index: int,
    asset_dir: Path,
    md_base: Path,
    existing_assets: list[AssetInfo],
) -> list[AssetInfo]:
    page_rect = page.rect
    page_area = rect_area(page_rect)
    clusters = cluster_rects(drawing_rects(page), page_rect)
    assets: list[AssetInfo] = []
    counter = 0

    for rect in sorted(clusters, key=lambda r: (r.y0, r.x0)):
        if rect.width < MIN_DIAGRAM_WIDTH_PT or rect.height < MIN_DIAGRAM_HEIGHT_PT:
            continue
        if page_area and rect_area(rect) / page_area < MIN_DIAGRAM_AREA_RATIO:
            continue
        if overlaps_existing_asset(rect, existing_assets):
            continue

        counter += 1
        asset_id = f"page-{page_index + 1:03d}-diagram-{counter:02d}"
        path = asset_dir / f"{asset_id}.png"
        pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), clip=rect, alpha=False)
        try:
            pix.save(path)
        finally:
            pix = None

        assets.append(
            AssetInfo(
                id=asset_id,
                asset_type="diagram",
                page=page_index + 1,
                bbox=rect_to_tuple(rect),
                path=path,
                relative_path=relative_to(path, md_base),
                width=round(float(rect.width), 2),
                height=round(float(rect.height), 2),
                source="page-render-crop",
                near_text=text_near_rect(page, rect),
            )
        )
    return assets


def asset_placeholder(asset: AssetInfo) -> str:
    return (
        "```d2\n"
        "# TODO: AI redraw this diagram in D2.\n"
        f"# Source image: {asset.relative_path}\n"
        f"# Source page: {asset.page}\n"
        f"# Source bbox: {asset.bbox}\n"
        "```\n"
    )


def page_text_to_markdown(page: fitz.Page, page_links: list[LinkInfo]) -> str:
    text_dict = page.get_text("dict")
    blocks = sort_blocks(text_dict.get("blocks", []))
    body_size = median_font_size(blocks)
    output: list[str] = []

    for block in blocks:
        if block.get("type") != 0:
            continue
        lines = [
            format_line_as_markdown(line, body_size, page_links)
            for line in block.get("lines", [])
        ]
        lines = [line for line in lines if line]
        if not lines:
            continue

        if len(lines) == 1:
            output.append(lines[0])
            continue

        block_rect = block_bbox(block)
        line_heights = [line_bbox(line).height for line in block.get("lines", []) if get_line_text(line)]
        avg_height = sum(line_heights) / len(line_heights) if line_heights else 0
        compact = avg_height and block_rect.height / max(len(lines), 1) <= avg_height * 1.6
        if compact:
            output.append("\n".join(lines))
        else:
            output.extend(lines)

    return "\n\n".join(output).strip()


def link_list_markdown(page_links: list[LinkInfo]) -> str:
    if not page_links:
        return ""
    lines = ["### Links"]
    for idx, link in enumerate(page_links, start=1):
        label = link.text or link.kind
        lines.append(f"- [{markdown_escape_link_text(label)}]({link.target})")
    return "\n".join(lines)


def assets_markdown(assets: list[AssetInfo]) -> str:
    if not assets:
        return ""
    lines = ["### Extracted visual assets"]
    for asset in assets:
        lines.append(f"<!-- asset: {asset.id}; type: {asset.asset_type}; source: {asset.source} -->")
        lines.append(asset_placeholder(asset).rstrip())
    return "\n\n".join(lines)


def manifest_asset(asset: AssetInfo) -> dict[str, Any]:
    return {
        "id": asset.id,
        "type": asset.asset_type,
        "page": asset.page,
        "bbox": asset.bbox,
        "width_pt": asset.width,
        "height_pt": asset.height,
        "path": asset.relative_path,
        "source": asset.source,
        "near_text": asset.near_text,
        "d2_placeholder": True,
    }


def manifest_link(link: LinkInfo) -> dict[str, Any]:
    return {
        "page": link.page,
        "bbox": link.bbox,
        "kind": link.kind,
        "target": link.target,
        "text": link.text,
    }


def convert_pdf(args: argparse.Namespace) -> tuple[Path, Path, list[AssetInfo], list[LinkInfo]]:
    out_path, manifest_path, asset_dir = ensure_output_paths(args)
    pdf_path = args.pdf.resolve()
    md_base = out_path.parent

    doc = fitz.open(pdf_path)
    try:
        page_indexes = parse_page_range(args.page_range, doc.page_count)
        title = normalize_text(doc.metadata.get("title") or "") or pdf_path.stem
        markdown_parts: list[str] = [f"# {title}"]
        all_assets: list[AssetInfo] = []
        all_links: list[LinkInfo] = []
        page_entries: list[dict[str, Any]] = []

        for page_index in page_indexes:
            page = doc.load_page(page_index)
            page_links = extract_links(page, page_index + 1)
            embedded_assets = save_embedded_images(doc, page, page_index, asset_dir, md_base)
            diagram_assets = render_diagram_crops(page, page_index, asset_dir, md_base, embedded_assets)
            page_assets = embedded_assets + diagram_assets

            all_links.extend(page_links)
            all_assets.extend(page_assets)

            page_parts = [f"## Page {page_index + 1}", f'<a id="page-{page_index + 1}"></a>']
            text_md = page_text_to_markdown(page, page_links)
            if text_md:
                page_parts.append(text_md)
            links_md = link_list_markdown(page_links)
            if links_md:
                page_parts.append(links_md)
            assets_md = assets_markdown(page_assets)
            if assets_md:
                page_parts.append(assets_md)
            markdown_parts.append("\n\n".join(page_parts).strip())

            page_entries.append(
                {
                    "page": page_index + 1,
                    "assets": [asset.id for asset in page_assets],
                    "links": [manifest_link(link) for link in page_links],
                    "text_chars": len(text_md),
                }
            )

        manifest = {
            "document": relative_to(pdf_path, Path.cwd()),
            "output": relative_to(out_path, Path.cwd()),
            "asset_dir": relative_to(asset_dir, Path.cwd()),
            "page_count": doc.page_count,
            "processed_pages": [page + 1 for page in page_indexes],
            "assets": [manifest_asset(asset) for asset in all_assets],
            "links": [manifest_link(link) for link in all_links],
            "pages": page_entries,
        }

        out_path.write_text("\n\n".join(markdown_parts).rstrip() + "\n", encoding="utf-8")
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return out_path, manifest_path, all_assets, all_links
    finally:
        doc.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert one PDF to Markdown and extract images/diagram crops for D2 redrawing.",
    )
    parser.add_argument("pdf", type=Path, help="Input PDF file.")
    parser.add_argument("--out", type=Path, help="Output Markdown path. Defaults to the PDF path with .md suffix.")
    parser.add_argument(
        "--asset-dir",
        type=Path,
        help="Directory for extracted PNG assets. Defaults to <pdf-dir>/assets/<pdf-slug>.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing Markdown, manifest, and asset files with the same names.",
    )
    parser.add_argument(
        "--page-range",
        help="1-based pages to process, for example '1-3,5'. Defaults to all pages.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        out_path, manifest_path, assets, links = convert_pdf(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote Markdown: {out_path}")
    print(f"Wrote manifest: {manifest_path}")
    print(f"Extracted assets: {len(assets)}")
    print(f"Extracted links: {len(links)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
