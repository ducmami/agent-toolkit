# scex-jira

CLI thân thiện với AI để gọi **Jira Data Center REST API**. Script cung cấp lệnh có cấu trúc, output JSON nhất quán, streaming NDJSON cho danh sách lớn, và export schema tool cho agent.

## Yêu cầu

- Python 3.10+
- Jira Data Center / Server với Personal Access Token (PAT)
- Quyền API tương ứng với thao tác bạn thực hiện

## Cài đặt

```bash
cd scripts/scex-jira
pip install -r requirements.txt
```

## Cấu hình

Tạo file `.env` tại **thư mục gốc repo** (`agent-toolkit/.env`):

```env
SCEX_JIRA_PAT=your-personal-access-token
SCEX_JIRA_API_URL=https://jira.example.com
```

| Biến | Mô tả |
|------|--------|
| `SCEX_JIRA_PAT` | Personal Access Token (Bearer auth) |
| `SCEX_JIRA_API_URL` | URL gốc Jira (`https://jira.example.com`), có hoặc không có `/rest` / `/rest/api` / `/rest/api/2` |

Có thể ghi đè bằng flag global:

```bash
python scex_jira.py --pat "$TOKEN" --api-url "https://jira.example.com" server info
```

Runtime mặc định dùng **`/rest/api/2`** (wiki markup, phù hợp Data Center). Swagger đi kèm là bản Cloud (`/rest/api/3`, ADF) — chỉ để `openapi list|describe`, không phải đường dẫn runtime. Dùng `--api-version 3` nếu gọi Jira Cloud.

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
| `--api-version` | `2` (mặc định, DC) hoặc `3` (Cloud) |

## Định dạng output

### Mặc định

Thành công: in `data` dạng JSON đẹp (hoặc text). Lỗi: in `message` ra stderr, kèm `hint` nếu có.

### `--json`

Mọi kết quả bọc trong envelope `CliResult`:

```json
{
  "status": "ok",
  "data": { ... },
  "meta": { "command": "issue get", "duration_ms": 120 }
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
    "hint": "Set SCEX_JIRA_PAT in .env or pass --pat <token>."
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
python scex_jira.py --help
python scex_jira.py <command> --help
```

### `schema` — Export tool cho AI agent

```bash
python scex_jira.py schema
python scex_jira.py schema --format openai
python scex_jira.py schema --format anthropic
```

Trả về định nghĩa tool (`TOOL_REGISTRY`) để đăng ký với OpenAI Functions, Anthropic Tools, hoặc agent framework khác.

### `openapi` — Duyệt swagger đi kèm

Swagger Jira Cloud: `platform-swagger-v3.v3.json`, `software-swagger.v3.json`.

Trên SCEX Data Center, map path `/rest/api/3/...` → `/rest/api/2/...`. Description/comment là **wiki markup**, không phải ADF.

```bash
python scex_jira.py openapi list
python scex_jira.py openapi list --spec platform --tag Issue
python scex_jira.py openapi describe getIssue
```

### `request` — Gọi REST tùy ý

```bash
python scex_jira.py request GET /rest/api/2/issue/ITSD-133
python scex_jira.py request GET /rest/api/2/issue/ITSD-133 --query expand=renderedFields
python scex_jira.py request POST /rest/api/2/search --body @query.json
python scex_jira.py request DELETE /rest/api/2/issue/ITSD-133 --force
```

Body: `--body-json`, `--body-stdin`, hoặc `--body @file.json` (chỉ dùng một trong ba).

Path `/secure/...` được gọi qua site root (không prefix REST). Path `/rest/agile/...` đi Agile API. Dùng `--output-file` để lưu binary.

### `server info`

`GET /rest/api/2/serverInfo` (fallback `/rest/api/3/serverInfo` nếu 404).

### `search jql`

```bash
python scex_jira.py search jql "key = ITSD-133"
python scex_jira.py search jql "project = ITSD AND text ~ \"vpn\"" --limit 10 --fields key,fields.summary
python scex_jira.py --stream search jql "project = ITSD" --all
```

`--issue-fields` là param `fields` của Jira Search API. `--fields` là projection dotted path trên JSON trả về. `--expand` mặc định `renderedFields`.

### `issue`

| Lệnh | Mô tả |
|------|--------|
| `issue get <key-or-url>` | Lấy issue (key, id, hoặc URL browse) |
| `issue resolve-url <url>` | Trích issue key từ URL (không cần PAT) |
| `issue export <key-or-url>` | Export JSON + description + comments + attachments |
| `issue create` | Tạo issue |
| `issue update <key-or-url>` | Cập nhật issue |
| `issue delete <key-or-url>` | Xóa issue (cần `--force`) |
| `issue assign <key-or-url>` | Assign theo username DC (`--name`) hoặc `--unassign` |

**Lấy nội dung ticket từ browse URL:**

```bash
python scex_jira.py issue get ITSD-133
python scex_jira.py issue get "https://jira.scex.com.vn/browse/ITSD-133"
python scex_jira.py issue get ITSD-133 --fields key,fields.summary,renderedFields.description
```

Mặc định `expand=renderedFields,names,changelog`. Nếu issue có nhiều comment hơn một trang, CLI tự gọi thêm API comment và gộp đủ.

**Resolve URL (không cần credential):**

```bash
python scex_jira.py issue resolve-url "https://jira.scex.com.vn/browse/ITSD-133"
```

**Export toàn bộ issue:**

```bash
python scex_jira.py issue export ITSD-133
python scex_jira.py issue export "https://jira.scex.com.vn/browse/ITSD-133" --skip-attachments
python scex_jira.py issue export ITSD-133 --output-dir ./export/ITSD-133
```

Thư mục mặc định: `scripts/output/scex-jira/<KEY>/`

- `issue.json` — response API đầy đủ
- `description.html` — `renderedFields.description` (fallback wiki)
- `description.wiki` — `fields.description` nếu là string
- `comments.json` — toàn bộ comment
- `attachments.json` — index attachment
- `manifest.json` — tóm tắt export
- file attachment (nếu không `--skip-attachments`)

**Tạo / cập nhật (wiki markup trên API v2, không ADF):**

```bash
python scex_jira.py issue create --project ITSD --issuetype Task --summary "VPN down" --description "Cannot connect"
python scex_jira.py issue update ITSD-133 --summary "VPN restored"
python scex_jira.py issue assign ITSD-133 --name johndoe
```

### `comment`

```bash
python scex_jira.py comment list ITSD-133 --all
python scex_jira.py comment get ITSD-133 10100
python scex_jira.py comment add ITSD-133 --text "looks good"
python scex_jira.py comment update ITSD-133 10100 --text "updated"
python scex_jira.py comment delete ITSD-133 10100 --force
```

### `attachment`

```bash
python scex_jira.py attachment list ITSD-133
python scex_jira.py attachment upload ITSD-133 --file ./screenshot.png
python scex_jira.py attachment download ITSD-133 10000 --output ./screenshot.png
python scex_jira.py attachment download-all ITSD-133
```

Upload gửi multipart field `file` kèm header `X-Atlassian-Token: no-check`.

### `transition`

```bash
python scex_jira.py transition list ITSD-133
python scex_jira.py transition do ITSD-133 --transition-id 5
```

### `user`

```bash
python scex_jira.py user current
python scex_jira.py user search --query jane
```

### `project`

```bash
python scex_jira.py project list
python scex_jira.py project get ITSD
```

Không CRUD project admin. Jira Service Management (`/rest/servicedeskapi`) không có lệnh riêng — dùng `request`.

### `board` / `sprint`

Agile đọc qua `/rest/agile/1.0` (cần Jira Software):

```bash
python scex_jira.py board list --project-key-or-id ITSD
python scex_jira.py board get 12
python scex_jira.py sprint list --board-id 12 --state active
python scex_jira.py sprint get 34
```

## Ví dụ workflow

**Kiểm tra kết nối:**

```bash
python scex_jira.py --json user current
```

**Từ URL browse → đọc nội dung → export:**

```bash
python scex_jira.py issue resolve-url "https://jira.scex.com.vn/browse/ITSD-133"
python scex_jira.py issue get ITSD-133 --fields key,fields.summary,fields.status,renderedFields.description
python scex_jira.py issue export ITSD-133
# → scripts/output/scex-jira/ITSD-133/
```

**Tìm issue rồi đọc:**

```bash
python scex_jira.py search jql "project = ITSD AND summary ~ \"VPN\"" --fields key,fields.summary
python scex_jira.py issue get ITSD-133
```

## Tích hợp AI agent

1. Chạy `schema --format openai` (hoặc `anthropic`) để lấy tool definitions.
2. Agent gọi CLI qua subprocess, luôn dùng `--json` để parse output ổn định.
3. Với dataset lớn, dùng `--stream --all` thay vì paginate thủ công.
4. Thao tác xóa: agent phải truyền `--force` sau khi xác nhận với người dùng.

Ví dụ wrapper tối thiểu:

```bash
python scex_jira.py --json issue get ITSD-133
```

## Mã thoát (exit code)

| Code | Ý nghĩa |
|------|---------|
| `0` | Thành công |
| `1` | Lỗi runtime / HTTP / network |
| `2` | Lỗi usage / validation |
| `3` | Lỗi cấu hình (thiếu PAT, URL, v.v.) |

## Ghi chú

- CLI nhắm tới **Jira Data Center** (`/rest/api/2`), không phải Jira Cloud ADF.
- `renderedFields` / `renderedBody` cho HTML; `fields.description` / comment `body` là wiki markup trên v2.
- Assign DC dùng `--name <username>`, không phải `accountId`.
- `--fields key,fields.summary,renderedFields.description` hỗ trợ dotted path để giảm token.
- Thao tác phá hủy (`issue delete`, `comment delete`, `request DELETE`) yêu cầu `--force`.
