# doconvert

CLI / library Python để convert tài liệu giữa các định dạng. Hiện implement **`confluence`**: GitHub Flavored Markdown → XHTML fragment cho Confluence Data Center `body.storage`.

Các lệnh `mermaid` / `markdown` đã có `--help` nhưng chưa implement (stub).

## Yêu cầu

- Python 3.10+
- Dependencies trong `requirements.txt`

## Cài đặt

```powershell
cd scripts/doconvert
python -m pip install -r requirements.txt
```

## Cách dùng (CLI)

```powershell
python scripts/doconvert/doconvert.py --help
python scripts/doconvert/doconvert.py confluence --help
```

### `confluence` — Markdown → Confluence body.storage

```powershell
python scripts/doconvert/doconvert.py confluence path\to\doc.md path\to\out.html

# TOC macro + base URL cho ảnh relative + theme mermaid
python scripts/doconvert/doconvert.py confluence doc.md out.html --toc `
  --image-base-url https://cdn.example.com `
  --mermaid-theme default `
  --mermaid-bg "!white"
```

| Flag               | Mô tả                                                     |
| ------------------ | --------------------------------------------------------- |
| `input`            | File `.md` / `.markdown` / `.mdx`                         |
| `output`           | File XHTML fragment (UTF-8)                               |
| `--toc`            | Chèn macro TOC Confluence ở đầu body                      |
| `--image-base-url` | Prefix cho `src` ảnh relative (không áp dụng mermaid.ink) |
| `--mermaid-theme`  | `default` \| `neutral` \| `dark` \| `forest`              |
| `--mermaid-bg`     | Query `bgColor` mermaid.ink (vd `!white`, `FFFFFF`)       |
| `--no-code-macro`  | Giữ `<pre><code>` thay vì macro `code` (debug)            |

### Mapping GFM → storage

| Markdown                                | Confluence storage                                         |
| --------------------------------------- | ---------------------------------------------------------- |
| Heading, table, list, link, bold/italic | XHTMLy tương đương                                          |
| Fence thường (`python`, `json`, …)      | Macro `code` + CDATA                                       |
| Fence `mermaid`                         | `<ac:image>` mermaid.ink PNG + macro `expand` chứa source (`code`) |
| Task list `- [ ]` / `- [x]`             | `ac:task-list` / `ac:task`                                 |
| `<img src="...">`                       | `<ac:image><ri:url …/></ac:image>`                         |
| GFM callout `> [!NOTE]` … / `/// note`  | Macro `info` / `tip` / `note` / `warning` + title          |
| YAML frontmatter                        | Bỏ trước khi convert                                       |

Callout mapping (severity): `NOTE`→`info`, `TIP`→`tip`, `IMPORTANT`→`note`, `WARNING`/`CAUTION`→`warning` (title phân biệt Warning vs Caution). Cũng hỗ trợ cú pháp pymdown `/// note` / `/// tip` / …

Wiki (browser/server) phải truy cập được `mermaid.ink` để hiển thị diagram.

### Ghép với scex-confluence

```powershell
python scripts/doconvert/doconvert.py confluence doc.md .temp/body.storage.html

# PowerShell: đọc file vào --body-storage
$body = Get-Content -Raw .temp/body.storage.html
python scripts/scex-confluence/scex_confluence.py content create `
  --title "My Page" --space-key DEV --body-storage $body
```

### Stub

```powershell
python scripts/doconvert/doconvert.py mermaid diagram.mmd out.png --fileType png
# ERROR: not implemented — dùng scripts/mmd2image

python scripts/doconvert/doconvert.py markdown doc.md out.html --to html
# ERROR: not implemented
```

## Library

Từ script sibling dưới `scripts/` (hoặc `scripts` trên `PYTHONPATH`):

```python
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from doconvert import convert_markdown_to_storage

xhtml = convert_markdown_to_storage(
    Path("doc.md").read_text(encoding="utf-8"),
    toc=True,
    mermaid_theme="default",
)
```

Public API: `convert_markdown_to_storage`.
