"""Cloudflare R2 upload helpers (library + CLI).

Typical import from a sibling script under ``scripts/``::

    import sys
    from pathlib import Path

    SCRIPTS_DIR = Path(__file__).resolve().parent.parent
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    from r2tool import upload
"""

from .r2tool import (
    R2_PREFIX,
    R2Config,
    load_config,
    make_client,
    presign_get_url,
    public_url_for,
    share,
    upload,
    upload_file,
)

__all__ = [
    "R2_PREFIX",
    "R2Config",
    "load_config",
    "make_client",
    "presign_get_url",
    "public_url_for",
    "share",
    "upload",
    "upload_file",
]
