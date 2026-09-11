# mmd2image

Chuyển diagram Mermaid sang file SVG hoặc PNG bằng `@mermaid-js/mermaid-cli` (`mmdc`).

## Yêu cầu

- Python 3.10+
- Node.js (kèm `npm` / `npx`)

## Cài đặt

Ưu tiên cài **global** (một lần / máy — mọi repo dùng được nếu `mmdc` có trên PATH):

```powershell
npm install -g @mermaid-js/mermaid-cli
```

Hoặc cài **local** trong thư mục script:

```powershell
cd scripts/mmd2image
npm install
```

Script ưu tiên `mmdc` trên PATH; không có thì dùng `npx --no-install mmdc` với `node_modules` local.

Lần đầu có thể chậm vì Puppeteer tải Chromium.

## Cách dùng

```powershell
python scripts/mmd2image/mmd2image.py path\to\file.md
python scripts/mmd2image/mmd2image.py path\to\diagram.mmd
python scripts/mmd2image/mmd2image.py path\to\diagram.mmd --fileType png
python scripts/mmd2image/mmd2image.py path\to\diagram.mmd --fileType png --background "#FFFFFF"
```

### Đầu vào

| Extension | Cách xử lý |
| --------- | ---------- |
| `.mmd`, `.mermaid` | Toàn bộ file = 1 diagram |
| `.md`, `.markdown`, `.mdx` | Mỗi fence ` ```mermaid ` → 1 diagram |

### Đầu ra (mặc định)

Cùng thư mục với file nguồn:

```text
images/<tên-file>/image1.svg
images/<tên-file>/image2.svg
...
```

Với `--fileType png`:

```text
images/<tên-file>/image1.png
images/<tên-file>/image2.png
...
```

Ví dụ:

- `docs/arch.md` (3 fence) → `docs/images/arch/image1.svg` … `image3.svg`
- `docs/flow.mmd` → `docs/images/flow/image1.svg`
- `docs/flow.mmd --fileType png` → `docs/images/flow/image1.png`

### Tùy chọn

| Flag | Mô tả |
| ---- | ----- |
| `--out-dir` | Thư mục xuất (ghi đè mặc định) |
| `--fileType` | Định dạng xuất: `svg` (mặc định) hoặc `png` |
| `--background` | Màu nền (`mmdc -b`). Mặc định `transparent`. Dùng kèm `--fileType`; ví dụ `"#FFFFFF"` (nên quote trong PowerShell) |

## Ví dụ với fixture tạm

```powershell
python scripts/mmd2image/mmd2image.py .temp/scex-sso.mmd
python scripts/mmd2image/mmd2image.py .temp/scex-sso.mmd --fileType png
python scripts/mmd2image/mmd2image.py .temp/scex-sso.mmd --fileType png --background "#FFFFFF"
python scripts/mmd2image/mmd2image.py .temp/scex-rar.md
```

Kỳ vọng:

- `.temp/images/scex-sso/image1.svg`
- `.temp/images/scex-sso/image1.png` (khi `--fileType png`)
- `.temp/images/scex-rar/image1.svg` … `image10.svg`
