# PDF to Markdown + D2 asset extraction

This tool converts one PDF into a Markdown draft and extracts visual assets so
an AI model can redraw architecture diagrams, models, and charts in D2.

## Setup

```powershell
python -m pip install -r scripts/pdf2md/requirements.txt
```

## Usage

Convert one PDF and write the Markdown file next to the source PDF:

```powershell
python scripts/pdf2md/convert_pdf.py "hld/remi/[approved] Onboarding flow - high level design - Onboarding Architecture - v1.0.3.pdf"
```

Overwrite existing output:

```powershell
python scripts/pdf2md/convert_pdf.py "hld/remi/accounting-v1.0.0.pdf" --overwrite
```

Process selected pages only:

```powershell
python scripts/pdf2md/convert_pdf.py "hld/remi/trading-v1.0.1.pdf" --page-range "1-3,7"
```

Write to a custom Markdown path and asset directory:

```powershell
python scripts/pdf2md/convert_pdf.py "hld/remi/reporting-v1.0.0.pdf" --out "tmp/reporting.md" --asset-dir "tmp/reporting-assets"
```

## Output

For a source PDF named `example.pdf`, default output is:

- `example.md`: Markdown draft with text, links, and D2 placeholders.
- `example.assets.json`: manifest with page, link, bbox, source image, and nearby text metadata.
- `assets/example/...png`: extracted embedded images and rendered vector diagram crops.

Each visual asset is represented in Markdown like this:

```d2
# TODO: AI redraw this diagram in D2.
# Source image: assets/example/page-001-diagram-01.png
# Source page: 1
# Source bbox: (10.0, 20.0, 300.0, 180.0)
```

The generated D2 blocks are placeholders. A later AI/review step should replace
them with real D2 while keeping the source image path for traceability if useful.
