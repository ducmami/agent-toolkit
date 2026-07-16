# scex-confluence

CLI thân thiện với AI để gọi **Confluence Data Center REST API**. Script cung cấp lệnh có cấu trúc, output JSON nhất quán, streaming NDJSON cho danh sách lớn, và export schema tool cho agent.

## Yêu cầu

- Python 3.10+
- Confluence Data Center với Personal Access Token (PAT)
- Quyền API tương ứng với thao tác bạn thực hiện

## Cài đặt

```bash
cd scripts/scex-confluence
pip install -r requirements.txt
```

## Cấu hình

Tạo file `.env` tại **thư mục gốc repo** (`agent-toolkit/.env`):

```env
SCEX_CONFLUENCE_PAT=your-personal-access-token
SCEX_CONFLUENCE_API_URL=https://wiki.example.com/confluence
```

| Biến | Mô tả |
|------|--------|
| `SCEX_CONFLUENCE_PAT` | Personal Access Token (Bearer auth) |
| `SCEX_CONFLUENCE_API_URL` | URL gốc Confluence, có hoặc không có `/rest/api` |

Có thể ghi đè bằng flag global:

```bash
python scex_confluence.py --pat "$TOKEN" --api-url "https://wiki.example.com/confluence" server info
```

## Tùy chọn global

| Flag | Mô tả |
|------|--------|
| `--json` | Output envelope `CliResult` dạng JSON |
| `--quiet` | Ẩn output không cần thiết |
| `--stream` | Stream NDJSON (mỗi dòng một event) |
| `--pat` | PAT (ghi đè env) |
| `--api-url` | URL API (ghi đè env) |
| `--timeout` | Timeout HTTP (mặc định: 30 giây) |
| `--force` | Bắt buộc cho thao tác xóa / DELETE |

## Định dạng output

### Mặc định

Thành công: in `data` dạng JSON đẹp (hoặc text). Lỗi: in `message` ra stderr, kèm `hint` nếu có.

### `--json`

Mọi kết quả bọc trong envelope `CliResult`:

```json
{
  "status": "ok",
  "data": { ... },
  "meta": { "command": "content get", "duration_ms": 120 }
}
```

Lỗi:

```json
{
  "status": "error",
  "error": {
    "code": "MISSING_PAT",
    "category": "config",
    "message": "Missing Personal Access Token.",
    "hint": "Set SCEX_CONFLUENCE_PAT in .env or pass --pat <token>."
  },
  "meta": { "command": "config" }
}
```

### `--stream`

Dùng cho lệnh list/search có phân trang. Mỗi dòng là một JSON event:

- `{"event": "item", "data": ...}` — một bản ghi
- `{"event": "progress", "data": {"page": 1, "fetched": 25}}` — tiến độ
- `{"event": "done", "status": "ok", "meta": {...}}` — kết thúc
- `{"event": "error", ...}` — lỗi

Kết hợp `--stream` với `--all` để lấy toàn bộ trang.

## Lệnh

```bash
python scex_confluence.py --help
python scex_confluence.py <command> --help
```

### `schema` — Export tool cho AI agent

```bash
python scex_confluence.py schema
python scex_confluence.py schema --format openai
python scex_confluence.py schema --format anthropic
```

Trả về định nghĩa tool (`TOOL_REGISTRY`) để đăng ký với OpenAI Functions, Anthropic Tools, hoặc agent framework khác.

### `openapi` — Duyệt swagger đi kèm

Swagger Confluence Data Center 9.2.1: `9.2.1.swagger.v3.json`

```bash
python scex_confluence.py openapi list
python scex_confluence.py openapi list --tag Content
python scex_confluence.py openapi describe getContentById
```

### `request` — Gọi REST tùy ý

```bash
python scex_confluence.py request GET /rest/api/content/123
python scex_confluence.py request GET /download/attachments/123/file.png --output-file ./file.png
python scex_confluence.py request POST /rest/api/content \
  --body @page.json
python scex_confluence.py request DELETE /rest/api/content/123 --force
```

Body: `--body-json`, `--body-stdin`, hoặc `--body @file.json` (chỉ dùng một trong ba).

Path `/download/...` được gọi qua site root (không prefix `/rest/api`). Dùng `--output-file` để lưu binary; HTTP lỗi sẽ fail thay vì ghi file rỗng.

### `server info`

Thông tin server Confluence.

### `search cql`

```bash
python scex_confluence.py search cql "type=page and space=DEV"
python scex_confluence.py search cql "text ~ \"release notes\"" --limit 10 --fields id,title,space
python scex_confluence.py --stream search cql "type=page" --all
```

### `content`

| Lệnh | Mô tả |
|------|--------|
| `content list` | Liệt kê content (lọc `--space-key`, `--type`, `--title`, `--status`) |
| `content get <id>` | Lấy content theo ID |
| `content resolve-url <url>` | Trích page ID từ URL Confluence hoặc chuỗi số |
| `content export <id>` | Export page + body + toàn bộ attachment ra thư mục |
| `content create` | Tạo page/blogpost |
| `content update <id>` | Cập nhật content |
| `content delete <id>` | Xóa content (cần `--force`) |

**Resolve URL → lấy page ID:**

```bash
python scex_confluence.py content resolve-url \
  "https://confluence.example.com/spaces/DEV/pages/123456/My-Page"
```

**Export toàn bộ page (metadata, body HTML, attachments):**

```bash
# Mặc định lưu vào scripts/output/scex-confluence/<CR-XXX-id>/
python scex_confluence.py content export 10551516
python scex_confluence.py content export 10551516 --skip-attachments

# Ghi đè thư mục đích
python scex_confluence.py content export 123456 --output-dir ./export/my-page
```

**Thư mục output mặc định** (khi không truyền `--output-dir`):

- Base: `scripts/output/scex-confluence/` (ngang hàng với `scripts/scex-confluence/`)
- Tên thư mục con: `<CR-XXX>-<page_id>` nếu tiêu đề page chứa mã `CR-XXX`, ngược lại chỉ dùng `<page_id>`
- Ví dụ page `CR-025` id `10551516` → `scripts/output/scex-confluence/CR-025-10551516/`

Kết quả trong thư mục export:

- `page.json` — metadata đầy đủ
- `body.html` — nội dung storage format
- `attachments.json` — index attachment từ API
- `manifest.json` — tóm tắt export
- Các file attachment (ảnh, docx, …)

**Tạo page nhanh:**

```bash
python scex_confluence.py content create \
  --title "Meeting Notes" \
  --space-key DEV \
  --body-storage "<p>Hello</p>"
```

**Cập nhật với auto-increment version:**

```bash
python scex_confluence.py content update 123456 \
  --auto-version \
  --body-storage "<p>Updated</p>"
```

### `space`

| Lệnh | Mô tả |
|------|--------|
| `space list` | Liệt kê space |
| `space get <key>` | Chi tiết space |
| `space create` | Tạo space (cần body JSON) |
| `space update <key>` | Cập nhật space |
| `space delete <key>` | Xóa space (cần `--force`) |

### `attachment`

```bash
python scex_confluence.py attachment list <content_id>
python scex_confluence.py attachment upload <content_id> --file ./doc.pdf --comment "v1"
python scex_confluence.py attachment download <content_id> <attachment_id> --output ./doc.pdf
python scex_confluence.py attachment download-all <content_id>
python scex_confluence.py attachment download-all <content_id> --output-dir ./attachments
```

`attachment download` dùng `_links.download` từ Confluence (path `/download/attachments/...`), không qua REST endpoint `/child/attachment/.../download` (thường trả 404 trên Data Center).

### `user`

```bash
python scex_confluence.py user current
python scex_confluence.py user list --limit 50
```

### `label`

```bash
python scex_confluence.py label recent
python scex_confluence.py label related documentation
```

### `convert body`

Chuyển đổi body content qua API Confluence:

```bash
python scex_confluence.py convert body --to storage --body @body.json
```

## Ví dụ workflow

**Kiểm tra kết nối:**

```bash
python scex_confluence.py --json user current
```

**Tìm page rồi đọc nội dung:**

```bash
python scex_confluence.py search cql "space=DEV and title=\"Architecture\"" --fields id,title
python scex_confluence.py content get <id> --fields id,title,body.storage
```

**Từ URL Confluence → export đầy đủ:**

```bash
python scex_confluence.py content resolve-url "https://confluence.example.com/spaces/DEV/pages/123456/..."
python scex_confluence.py content export 123456
# → scripts/output/scex-confluence/123456/ (hoặc CR-XXX-123456 nếu tiêu đề có mã CR)
```

**Stream toàn bộ page trong space:**

```bash
python scex_confluence.py --stream content list --space-key DEV --type page --all --fields id,title
```

## Tích hợp AI agent

1. Chạy `schema --format openai` (hoặc `anthropic`) để lấy tool definitions.
2. Agent gọi CLI qua subprocess, luôn dùng `--json` để parse output ổn định.
3. Với dataset lớn, dùng `--stream --all` thay vì paginate thủ công.
4. Thao tác xóa: agent phải truyền `--force` sau khi xác nhận với người dùng.

Ví dụ wrapper tối thiểu:

```bash
python scex_confluence.py --json search cql "type=page and space=DEV"
```

## Mã thoát (exit code)

| Code | Ý nghĩa |
|------|---------|
| `0` | Thành công |
| `1` | Lỗi runtime / HTTP / network |
| `2` | Lỗi usage / validation |
| `3` | Lỗi cấu hình (thiếu PAT, URL, v.v.) |

## Ghi chú

- CLI nhắm tới **Confluence Data Center**, không phải Confluence Cloud.
- Path API được chuẩn hóa tự động: có thể truyền `/content/123` hoặc `/rest/api/content/123`.
- Path site (`/download/`, `/s/`, `/plugins/`, `/images/`) không bị thêm prefix `/rest/api`.
- `--fields id,title,body.storage` hỗ trợ dotted path để giảm token khi agent chỉ cần một phần response.
- Thao tác phá hủy (`content delete`, `space delete`, `request DELETE`) yêu cầu `--force`.
