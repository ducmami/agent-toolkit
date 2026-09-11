---
name: doconvert
description: >-
  Use when converting Markdown (GFM) to Confluence Data Center body.storage
  XHTML, or when preparing page body for scex-confluence create/update.
  Triggers: doconvert, confluence storage, markdown to confluence, body.storage,
  mermaid.ink in Confluence. Do not use for local mermaid PNG via mmdc (use
  mmd2image) or Confluence REST API calls (use scex-confluence).
---

# doconvert

Convert documents for Confluence and related formats via
`scripts/doconvert/doconvert.py`.

## When to use

- User has GitHub Flavored Markdown and needs Confluence **storage format**
  (`body.storage`) to paste into REST create/update or `scex-confluence`.
- Markdown contains mermaid fenced code blocks that should render as images on
  the wiki (hotlinked via mermaid.ink PNG).

## When not to use

| Need | Use instead |
|------|-------------|
| Mermaid → local PNG/SVG files | `scripts/mmd2image/mmd2image.py` |
| Call Confluence REST (create page, upload attachment) | `scripts/scex-confluence` |
| Generic MD → PDF / standalone HTML | not implemented (`markdown` stub) |

## CLI

```powershell
python -m pip install -r scripts/doconvert/requirements.txt

python scripts/doconvert/doconvert.py confluence INPUT.md OUTPUT.html
python scripts/doconvert/doconvert.py confluence INPUT.md OUTPUT.html --toc `
  --image-base-url https://cdn.example.com `
  --mermaid-theme default
```

## Behavior (confluence)

1. Strip YAML frontmatter.
2. Render GFM with Python-Markdown + pymdown-extensions (no Pygments HTML spans).
   Callouts: `pymdownx.quotes` (`> [!NOTE]` …) and `pymdownx.blocks.admonition`
   (`/// note`).
3. Normal code fences → Confluence `code` macro.
4. Mermaid fences → `<ac:image>` with
   `https://mermaid.ink/img/pako:<encoded>?type=png`
   (pako = zlib+base64url of JSON state with code + theme), plus a collapsed
   Confluence `expand` macro titled "Mermaid source" wrapping the original
   fence as a `code` macro.
5. Task lists → `ac:task-list`; images → `ac:image` / `ri:url`.
6. Admonitions → Confluence macros (severity): `NOTE`→`info`, `TIP`→`tip`,
   `IMPORTANT`→`note`, `WARNING`/`CAUTION`→`warning` (title from callout type).
7. Output is an **XHTML fragment only** (no `<html>` / `<body>`).

**Requirement:** Confluence clients must reach `mermaid.ink` (no local render, no R2 upload).

## Pipe into scex-confluence

```powershell
python scripts/doconvert/doconvert.py confluence doc.md .temp/body.storage.html
$body = Get-Content -Raw .temp/body.storage.html
python scripts/scex-confluence/scex_confluence.py content create `
  --title "Title" --space-key DEV --body-storage $body
```

## Library

```python
from doconvert import convert_markdown_to_storage  # scripts/ on sys.path
xhtml = convert_markdown_to_storage(md_text, toc=True, mermaid_theme="default")
```

See [scripts/doconvert/README.md](../../scripts/doconvert/README.md) for full flags.
