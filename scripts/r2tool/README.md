# r2tool

CLI / library Python để upload file lên **Cloudflare R2** qua API tương thích S3 (`boto3`).

Lệnh: `upload` (public URL) và `share` (presigned GET URL, mặc định 7 ngày).

## Yêu cầu

- Python 3.10+
- Cloudflare R2 bucket + API token (Access Key ID / Secret Access Key)

## Cài đặt

```powershell
cd scripts/r2tool
pip install -r requirements.txt
```

## Cấu hình

Tạo file `.env` tại **thư mục gốc repo** (`agent-toolkit/.env`):

```env
R2_API_URL=
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET_NAME=
R2_PUBLIC_URL=
```

| Biến                   | Mô tả                                                                                                               |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `R2_API_URL`           | Endpoint S3 (không trailing slash), ví dụ `https://<account_id>.r2.cloudflarestorage.com` — dùng khi gọi API upload |
| `R2_ACCESS_KEY_ID`     | R2 Access Key ID                                                                                                    |
| `R2_SECRET_ACCESS_KEY` | R2 Secret Access Key                                                                                                |
| `R2_BUCKET_NAME`       | Tên bucket                                                                                                          |
| `R2_PUBLIC_URL`        | Base URL public (không trailing slash), ví dụ `https://pub-….r2.dev` — dùng để ghép link file sau upload            |

Prefix thư mục mặc định trong bucket là `R2_PREFIX = "markdown"`. Đổi bằng `--prefix` (CLI) hoặc `prefix=` (library), hoặc sửa hằng trong `r2tool.py`.

## Cách dùng (CLI)

```powershell
# Upload — tên object mặc định: markdown/<stem>-<yyyyMMdd>-<uuid8><ext>
python scripts/r2tool/r2tool.py upload path\to\file.md

# Upload với tên cố định (không gắn date/uuid)
python scripts/r2tool/r2tool.py upload path\to\file.md --name custom.md

# Upload sang folder khác trong bucket
python scripts/r2tool/r2tool.py upload path\to\file.md --prefix images

# Share — upload rồi trả presigned GET URL (mặc định 7 ngày, tối đa 7 ngày vì SigV4)
python scripts/r2tool/r2tool.py share path\to\file.md

# Share với thời hạn ngắn hơn
python scripts/r2tool/r2tool.py share path\to\file.md --expires-days 1
```

Hoặc (khi `scripts/` nằm trên `PYTHONPATH` / `sys.path`):

```powershell
python -m r2tool upload path\to\file.md
python -m r2tool share path\to\file.md
```

### Object key

| Trường hợp         | Key ví dụ                               |
| ------------------ | --------------------------------------- |
| Mặc định           | `markdown/image2-20260824-5cb87ff9.png` |
| `--name custom.md` | `markdown/custom.md`                    |
| `--prefix images`  | `images/image2-20260824-5cb87ff9.png`   |

### Output (`upload`)

```text
key: markdown/image2-20260824-5cb87ff9.png
url: https://pub-45729461dc1b43aea917e69dd81a3d50.r2.dev/markdown/image2-20260824-5cb87ff9.png
```

### Output (`share`)

`url` là presigned GET (SigV4), không phải `R2_PUBLIC_URL`. Thời hạn mặc định và tối đa là 7 ngày (`--expires-days` từ 1 đến 7).

```text
key: markdown/file-20260911-5cb87ff9.md
url: https://<account_id>.r2.cloudflarestorage.com/<bucket>/markdown/file-20260911-5cb87ff9.md?X-Amz-Algorithm=AWS4-HMAC-SHA256&...
expires: 7d
```

## Library

Từ script sibling dưới `scripts/` (ví dụ `scripts/doconvert/doconvert.py`):

```python
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from r2tool import share, upload

key, url = upload(Path("out.png"), prefix="images")
key, signed_url = share(Path("out.png"), prefix="images")
```

Reuse config/client khi upload nhiều file:

```python
from r2tool import load_config, make_client, share, upload

cfg = load_config()
client = make_client(cfg)
for path in paths:
    key, url = upload(path, cfg=cfg, client=client, prefix="images")
```

Public API: `upload`, `share`, `upload_file`, `presign_get_url`, `load_config`, `make_client`, `public_url_for`, `R2Config`, `R2_PREFIX`.
