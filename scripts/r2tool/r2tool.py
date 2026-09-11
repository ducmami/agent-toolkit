#!/usr/bin/env python3
"""Upload files to Cloudflare R2 via the S3-compatible API (boto3).

Usable as a CLI (`python r2tool.py upload …` / `share …`) or as a library
(`from r2tool import upload, share`).
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import boto3
from botocore.client import BaseClient, Config
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent

# Default bucket "folder" prefix for uploads — override via prefix= / --prefix.
R2_PREFIX = "markdown"

# SigV4 presigned GET lifetime (R2/AWS max is 7 days).
SECONDS_PER_DAY = 24 * 60 * 60
DEFAULT_SHARE_EXPIRES_SECONDS = 7 * SECONDS_PER_DAY
MAX_SHARE_EXPIRES_SECONDS = DEFAULT_SHARE_EXPIRES_SECONDS
MAX_SHARE_EXPIRES_DAYS = 7

REQUIRED_ENV = (
    "R2_API_URL",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
    "R2_PUBLIC_URL",
)


@dataclass(frozen=True)
class R2Config:
    api_url: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_url: str


def load_config() -> R2Config:
    load_dotenv(REPO_ROOT / ".env")
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing required env var(s): "
            + ", ".join(missing)
            + f". Set them in {REPO_ROOT / '.env'}."
        )
    return R2Config(
        api_url=os.environ["R2_API_URL"].strip().rstrip("/"),
        access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
        bucket_name=os.environ["R2_BUCKET_NAME"].strip(),
        public_url=os.environ["R2_PUBLIC_URL"].strip().rstrip("/"),
    )


def make_client(cfg: R2Config) -> BaseClient:
    return boto3.client(
        service_name="s3",
        endpoint_url=cfg.api_url,
        aws_access_key_id=cfg.access_key_id,
        aws_secret_access_key=cfg.secret_access_key,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def build_object_key(
    local_path: Path,
    name: str | None,
    prefix: str | None = None,
) -> str:
    if name:
        filename = Path(name).name
        if not filename or filename in (".", ".."):
            raise ValueError(f"Invalid --name: {name!r}")
    else:
        stem = local_path.stem
        suffix = local_path.suffix
        date = datetime.now().strftime("%Y%m%d")
        filename = f"{stem}-{date}-{uuid.uuid4().hex[:8]}{suffix}"

    resolved = (R2_PREFIX if prefix is None else prefix).strip("/")
    return f"{resolved}/{filename}" if resolved else filename


def public_url_for(cfg: R2Config, key: str) -> str:
    return f"{cfg.public_url}/{key}"


def _resolve_expires_in(expires_in: int | None) -> int:
    resolved = DEFAULT_SHARE_EXPIRES_SECONDS if expires_in is None else expires_in
    if resolved < 1 or resolved > MAX_SHARE_EXPIRES_SECONDS:
        raise ValueError(
            "expires_in must be between 1 and "
            f"{MAX_SHARE_EXPIRES_SECONDS} seconds (SigV4 max 7 days), got {resolved}."
        )
    return resolved


def presign_get_url(
    client: BaseClient,
    cfg: R2Config,
    key: str,
    expires_in: int | None = None,
) -> str:
    """Return a SigV4 presigned GET URL for an existing object key."""
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": cfg.bucket_name, "Key": key},
        ExpiresIn=_resolve_expires_in(expires_in),
    )


def upload_file(
    client: BaseClient,
    cfg: R2Config,
    local_path: Path,
    name: str | None,
    *,
    prefix: str | None = None,
    content_type: str | None = None,
) -> tuple[str, str]:
    if not local_path.is_file():
        raise FileNotFoundError(f"File not found: {local_path}")

    key = build_object_key(local_path, name, prefix=prefix)
    extra_args: dict[str, str] = {}
    if content_type:
        extra_args["ContentType"] = content_type
    if extra_args:
        client.upload_file(str(local_path), cfg.bucket_name, key, ExtraArgs=extra_args)
    else:
        client.upload_file(str(local_path), cfg.bucket_name, key)
    return key, public_url_for(cfg, key)


def upload(
    local_path: str | Path,
    *,
    name: str | None = None,
    prefix: str | None = None,
    content_type: str | None = None,
    cfg: R2Config | None = None,
    client: BaseClient | None = None,
) -> tuple[str, str]:
    """Upload a local file to R2. Returns ``(object_key, public_url)``.

    ``prefix=None`` uses :data:`R2_PREFIX`. Pass ``cfg`` / ``client`` to reuse
    a connection across multiple uploads. ``content_type=None`` guesses MIME
    from the file extension.
    """
    path = Path(local_path).resolve()
    resolved_cfg = cfg if cfg is not None else load_config()
    resolved_client = client if client is not None else make_client(resolved_cfg)

    mime = content_type
    if mime is None:
        guessed, _ = mimetypes.guess_type(str(path))
        mime = guessed

    return upload_file(
        resolved_client,
        resolved_cfg,
        path,
        name,
        prefix=prefix,
        content_type=mime,
    )


def share(
    local_path: str | Path,
    *,
    name: str | None = None,
    prefix: str | None = None,
    content_type: str | None = None,
    expires_in: int | None = None,
    cfg: R2Config | None = None,
    client: BaseClient | None = None,
) -> tuple[str, str]:
    """Upload a local file and return ``(object_key, presigned_get_url)``.

    ``expires_in=None`` uses :data:`DEFAULT_SHARE_EXPIRES_SECONDS` (7 days).
    Must be between 1 and :data:`MAX_SHARE_EXPIRES_SECONDS` (SigV4 cap).
    """
    resolved_cfg = cfg if cfg is not None else load_config()
    resolved_client = client if client is not None else make_client(resolved_cfg)
    resolved_expires = _resolve_expires_in(expires_in)
    key, _public_url = upload(
        local_path,
        name=name,
        prefix=prefix,
        content_type=content_type,
        cfg=resolved_cfg,
        client=resolved_client,
    )
    return key, presign_get_url(
        resolved_client,
        resolved_cfg,
        key,
        resolved_expires,
    )


def cmd_upload(args: argparse.Namespace) -> int:
    key, url = upload(
        args.path,
        name=args.name,
        prefix=args.prefix,
    )
    print(f"key: {key}")
    print(f"url: {url}")
    return 0


def cmd_share(args: argparse.Namespace) -> int:
    key, url = share(
        args.path,
        name=args.name,
        prefix=args.prefix,
        expires_in=args.expires_days * SECONDS_PER_DAY,
    )
    print(f"key: {key}")
    print(f"url: {url}")
    print(f"expires: {args.expires_days}d")
    return 0


def _expires_days_type(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: {value!r}") from exc
    if days < 1 or days > MAX_SHARE_EXPIRES_DAYS:
        raise argparse.ArgumentTypeError(
            f"must be an integer from 1 to {MAX_SHARE_EXPIRES_DAYS}"
        )
    return days


def _add_object_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "path",
        type=Path,
        help="Local file path to upload.",
    )
    parser.add_argument(
        "--name",
        help=(
            "Object filename under the configured prefix "
            f"(default: <stem>-<yyyyMMdd>-<uuid8><ext> under '{R2_PREFIX}/')."
        ),
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help=(
            f"Bucket folder prefix (default: '{R2_PREFIX}'). "
            "Use empty string for bucket root."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload files to Cloudflare R2 (S3-compatible API).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    upload_cmd = sub.add_parser("upload", help="Upload one local file to R2.")
    _add_object_args(upload_cmd)
    upload_cmd.set_defaults(func=cmd_upload)

    share_cmd = sub.add_parser(
        "share",
        help="Upload one local file and return a presigned GET URL.",
    )
    _add_object_args(share_cmd)
    share_cmd.add_argument(
        "--expires-days",
        type=_expires_days_type,
        default=MAX_SHARE_EXPIRES_DAYS,
        metavar="N",
        help=(
            f"Presigned URL lifetime in days "
            f"(default: {MAX_SHARE_EXPIRES_DAYS}, min: 1, max: {MAX_SHARE_EXPIRES_DAYS})."
        ),
    )
    share_cmd.set_defaults(func=cmd_share)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
