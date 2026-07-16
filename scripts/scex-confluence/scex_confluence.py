#!/usr/bin/env python3
"""AI-friendly CLI for Confluence Data Center REST API."""

from __future__ import annotations

import json
import re
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import typer
from dotenv import load_dotenv
from pydantic import BaseModel
from typer.core import TyperGroup

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
SWAGGER_PATH = SCRIPT_DIR / "9.2.1.swagger.v3.json"
DEFAULT_EXPORT_OUTPUT_BASE = SCRIPT_DIR.parent / "output" / "scex-confluence"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

DEFAULT_LIMIT = 25
DEFAULT_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ErrorCategory(str, Enum):
    CLIENT = "client"
    SERVER = "server"
    NETWORK = "network"
    VALIDATION = "validation"
    CONFIG = "config"


class CliError(BaseModel):
    code: str
    category: ErrorCategory
    message: str
    hint: Optional[str] = None
    http_status: Optional[int] = None
    details: Optional[Any] = None


class CliMeta(BaseModel):
    command: str
    http_status: Optional[int] = None
    duration_ms: Optional[int] = None


class CliResult(BaseModel):
    status: Literal["ok", "error"]
    data: Optional[Any] = None
    error: Optional[CliError] = None
    meta: CliMeta


# ---------------------------------------------------------------------------
# Global runtime context (set by Typer callback)
# ---------------------------------------------------------------------------


@dataclass
class RuntimeContext:
    json_output: bool = False
    quiet: bool = False
    stream: bool = False
    pat: Optional[str] = None
    api_url: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    force: bool = False
    command: str = ""


_ctx = RuntimeContext()


def get_ctx() -> RuntimeContext:
    return _ctx


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _write_line(text: str, *, err: bool = False) -> None:
    stream = sys.stderr if err else sys.stdout
    stream.write(text + "\n")
    stream.flush()


def emit(result: CliResult, *, exit_code: int = EXIT_OK) -> None:
    ctx = get_ctx()
    if ctx.json_output and not ctx.stream:
        _write_line(result.model_dump_json(exclude_none=True))
    elif result.status == "error" and result.error:
        msg = result.error.message
        if result.error.hint and not ctx.quiet:
            msg += f"\nHint: {result.error.hint}"
        _write_line(msg, err=True)
    elif result.status == "ok" and result.data is not None and not ctx.quiet:
        if isinstance(result.data, (dict, list)):
            _write_line(json.dumps(result.data, indent=2, ensure_ascii=False))
        else:
            _write_line(str(result.data))
    raise typer.Exit(exit_code)


def emit_stream_event(event: dict[str, Any]) -> None:
    _write_line(json.dumps(event, ensure_ascii=False))


def emit_stream_done(
    *,
    command: str,
    count: int = 0,
    duration_ms: int = 0,
    extra_meta: Optional[dict[str, Any]] = None,
) -> None:
    meta: dict[str, Any] = {"command": command, "count": count, "duration_ms": duration_ms}
    if extra_meta:
        meta.update(extra_meta)
    emit_stream_event({"event": "done", "status": "ok", "meta": meta})


def emit_stream_error(error: CliError, *, command: str) -> None:
    emit_stream_event(
        {
            "event": "error",
            "status": "error",
            "error": error.model_dump(exclude_none=True),
            "meta": {"command": command},
        }
    )
    raise typer.Exit(EXIT_ERROR)


def make_ok(data: Any, *, command: str, http_status: Optional[int] = None, duration_ms: int = 0) -> CliResult:
    return CliResult(
        status="ok",
        data=data,
        error=None,
        meta=CliMeta(command=command, http_status=http_status, duration_ms=duration_ms),
    )


def make_error(
    *,
    code: str,
    category: ErrorCategory,
    message: str,
    command: str,
    hint: Optional[str] = None,
    http_status: Optional[int] = None,
    details: Optional[Any] = None,
    exit_code: int = EXIT_ERROR,
) -> None:
    result = CliResult(
        status="error",
        data=None,
        error=CliError(
            code=code,
            category=category,
            message=message,
            hint=hint,
            http_status=http_status,
            details=details,
        ),
        meta=CliMeta(command=command),
    )
    ctx = get_ctx()
    if ctx.stream:
        emit_stream_error(result.error, command=command)  # type: ignore[arg-type]
    emit(result, exit_code=exit_code)


# ---------------------------------------------------------------------------
# Body resolution
# ---------------------------------------------------------------------------


def resolve_body(
    *,
    body_json: Optional[str],
    body_stdin: bool,
    body_file: Optional[str],
    command: str,
) -> Optional[Any]:
    sources = sum(
        [
            body_json is not None,
            body_stdin,
            body_file is not None,
        ]
    )
    if sources > 1:
        make_error(
            code="BODY_CONFLICT",
            category=ErrorCategory.VALIDATION,
            message="Only one of --body-json, --body-stdin, or --body may be used.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    raw: Optional[str] = None
    if body_json is not None:
        raw = body_json
    elif body_stdin:
        if sys.stdin.isatty():
            make_error(
                code="BODY_STDIN_EMPTY",
                category=ErrorCategory.VALIDATION,
                message="--body-stdin requires piped stdin input.",
                command=command,
                exit_code=EXIT_USAGE,
            )
        raw = sys.stdin.read()
    elif body_file is not None:
        path = body_file[1:] if body_file.startswith("@") else body_file
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            make_error(
                code="BODY_FILE_ERROR",
                category=ErrorCategory.VALIDATION,
                message=f"Cannot read body file: {exc}",
                command=command,
                exit_code=EXIT_USAGE,
            )

    if raw is None:
        return None

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        make_error(
            code="BODY_JSON_INVALID",
            category=ErrorCategory.VALIDATION,
            message=f"Invalid JSON body: {exc}",
            command=command,
            exit_code=EXIT_USAGE,
        )


def build_content_body(
    *,
    title: Optional[str],
    space_key: Optional[str],
    body_storage: Optional[str],
    content_type: str = "page",
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = deepcopy(existing) if existing else {}
    if title is not None:
        body["title"] = title
    if content_type:
        body["type"] = content_type
    if space_key is not None:
        body["space"] = {"key": space_key}
    if body_storage is not None:
        body["body"] = {
            "storage": {"value": body_storage, "representation": "storage"},
        }
    return body


# ---------------------------------------------------------------------------
# Field projection
# ---------------------------------------------------------------------------


def project_fields(data: Any, fields: Optional[str]) -> Any:
    if not fields or data is None:
        return data
    keys = [k.strip() for k in fields.split(",") if k.strip()]

    def get_path(obj: dict[str, Any], path: str) -> tuple[bool, Any]:
        current: Any = obj
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return False, None
            current = current[part]
        return True, current

    def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
        parts = path.split(".")
        current = obj
        for part in parts[:-1]:
            nested = current.setdefault(part, {})
            if not isinstance(nested, dict):
                nested = {}
                current[part] = nested
            current = nested
        current[parts[-1]] = value

    def pick(obj: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in keys:
            if "." in key:
                found, value = get_path(obj, key)
                if found:
                    set_path(result, key, value)
            elif key in obj:
                result[key] = obj[key]
        return result

    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            projected = dict(data)
            projected["results"] = [pick(item) if isinstance(item, dict) else item for item in data["results"]]
            return projected
        return pick(data)
    if isinstance(data, list):
        return [pick(item) if isinstance(item, dict) else item for item in data]
    return data


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def normalize_api_url(api_url: str) -> str:
    return api_url.rstrip("/")


def site_root(api_url: str) -> str:
    root = normalize_api_url(api_url)
    if root.endswith("/rest/api"):
        return root[: -len("/rest/api")]
    return root


SITE_PATH_PREFIXES = ("/download/", "/s/", "/plugins/", "/images/")


def is_site_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in SITE_PATH_PREFIXES)


def normalize_api_path(api_url: str, path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if is_site_path(path):
        return path
    root = normalize_api_url(api_url)
    if root.endswith("/rest/api"):
        if path.startswith("/rest/api"):
            return path[len("/rest/api") :] or "/"
        return path
    if path.startswith("/rest/api"):
        return path
    return f"/rest/api{path}"


def parse_confluence_page_id(url_or_id: str, *, command: str) -> str:
    text = url_or_id.strip()
    if text.isdigit():
        return text

    parsed = urlparse(text)
    path = parsed.path or text
    query = parse_qs(parsed.query)
    if "pageId" in query and query["pageId"][0].isdigit():
        return query["pageId"][0]

    for pattern in (r"/pages/(\d+)", r"/content/(\d+)"):
        match = re.search(pattern, path)
        if match:
            return match.group(1)

    make_error(
        code="INVALID_CONFLUENCE_URL",
        category=ErrorCategory.VALIDATION,
        message=f"Cannot extract page ID from: {url_or_id}",
        hint="Pass a numeric page ID or a Confluence URL containing /pages/<id> or ?pageId=<id>.",
        command=command,
        exit_code=EXIT_USAGE,
    )


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "attachment"


def export_output_dir_name(title: str | None, content_id: str) -> str:
    if title:
        match = re.search(r"CR-\d+", title, re.IGNORECASE)
        if match:
            return f"{match.group(0).upper()}-{content_id}"
    return content_id


def resolve_export_output_dir(
    content_id: str,
    *,
    output_dir: Path | None = None,
    title: str | None = None,
) -> Path:
    if output_dir is not None:
        return output_dir
    return DEFAULT_EXPORT_OUTPUT_BASE / export_output_dir_name(title, content_id)


def load_config() -> tuple[str, str]:
    load_dotenv(REPO_ROOT / ".env")
    ctx = get_ctx()
    pat = ctx.pat or __import__("os").environ.get("SCEX_CONFLUENCE_PAT")
    api_url = ctx.api_url or __import__("os").environ.get("SCEX_CONFLUENCE_API_URL")
    if not pat:
        make_error(
            code="MISSING_PAT",
            category=ErrorCategory.CONFIG,
            message="Missing Personal Access Token.",
            hint="Set SCEX_CONFLUENCE_PAT in .env or pass --pat <token>.",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    if not api_url:
        make_error(
            code="MISSING_API_URL",
            category=ErrorCategory.CONFIG,
            message="Missing Confluence API URL.",
            hint="Set SCEX_CONFLUENCE_API_URL in .env or pass --api-url <url>.",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        make_error(
            code="INVALID_API_URL",
            category=ErrorCategory.CONFIG,
            message=f"API URL must include scheme: {api_url}",
            hint="Example: https://wiki.example.com/confluence",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    return pat, api_url


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class ConfluenceClient:
    def __init__(self, pat: str, api_url: str, timeout: float) -> None:
        self.api_url = normalize_api_url(api_url)
        self.site_url = site_root(self.api_url)
        self._pat = pat
        self._timeout = timeout
        auth_headers = {"Authorization": f"Bearer {pat}"}
        self._client = httpx.Client(
            base_url=self.api_url,
            headers={**auth_headers, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._site_client = httpx.Client(
            base_url=self.site_url,
            headers=auth_headers,
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()
        self._site_client.close()

    def _path(self, path: str) -> str:
        return normalize_api_path(self.api_url, path)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        files: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        path = self._path(path)
        return self._send(self._client, method, path, params=params, json_body=json_body, headers=headers, files=files, content=content)

    def request_site(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        if not path.startswith("/"):
            path = "/" + path
        return self._send(self._site_client, method, path, params=params, headers=headers)

    def _send(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[dict[str, str]] = None,
        files: Optional[dict[str, Any]] = None,
        content: Optional[bytes] = None,
    ) -> httpx.Response:
        try:
            return client.request(
                method.upper(),
                path,
                params=params,
                json=json_body,
                headers=headers,
                files=files,
                content=content,
            )
        except httpx.TimeoutException as exc:
            make_error(
                code="NETWORK_TIMEOUT",
                category=ErrorCategory.NETWORK,
                message=f"Request timed out: {exc}",
                command=get_ctx().command,
            )
        except httpx.RequestError as exc:
            make_error(
                code="NETWORK_ERROR",
                category=ErrorCategory.NETWORK,
                message=str(exc),
                command=get_ctx().command,
            )

    def save_binary(self, response: httpx.Response, output: Path) -> None:
        if response.status_code >= 400:
            self._raise_http_error(response)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)

    def list_attachments(self, content_id: str, *, expand: Optional[str] = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if expand:
            params["expand"] = expand
        data = self.parse_response(
            self.request("GET", f"/rest/api/content/{content_id}/child/attachment", params=params or None)
        )
        if not isinstance(data, dict):
            return []
        results = data.get("results", [])
        return [item for item in results if isinstance(item, dict)]

    def get_attachment(self, attachment_id: str) -> dict[str, Any]:
        data = self.parse_response(
            self.request("GET", f"/rest/api/content/{attachment_id}", params={"expand": "version"})
        )
        if not isinstance(data, dict):
            make_error(
                code="ATTACHMENT_NOT_FOUND",
                category=ErrorCategory.CLIENT,
                message=f"Attachment {attachment_id} not found.",
                command=get_ctx().command,
            )
        return data

    def download_attachment(self, content_id: str, attachment_id: str, output: Path) -> dict[str, Any]:
        _ = content_id
        attachment = self.get_attachment(attachment_id)
        download_path = attachment.get("_links", {}).get("download")
        if not download_path:
            make_error(
                code="ATTACHMENT_DOWNLOAD_LINK_MISSING",
                category=ErrorCategory.CLIENT,
                message=f"Attachment {attachment_id} has no download link.",
                command=get_ctx().command,
            )

        if download_path.startswith("http://") or download_path.startswith("https://"):
            response = httpx.get(
                download_path,
                headers={"Authorization": f"Bearer {self._pat}"},
                timeout=self._timeout,
                follow_redirects=True,
            )
        else:
            response = self.request_site("GET", download_path)

        self.save_binary(response, output)
        return {
            "id": attachment.get("id", attachment_id),
            "title": attachment.get("title"),
            "saved": str(output),
            "bytes": len(response.content),
            "mediaType": attachment.get("extensions", {}).get("mediaType"),
        }

    def download_all_attachments(self, content_id: str, output_dir: Path) -> list[dict[str, Any]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        attachments = self.list_attachments(content_id, expand="version")
        saved: list[dict[str, Any]] = []
        used_names: dict[str, int] = {}
        for attachment in attachments:
            attachment_id = str(attachment.get("id", ""))
            title = str(attachment.get("title") or attachment_id)
            filename = sanitize_filename(title)
            if filename in used_names:
                used_names[filename] += 1
                stem = Path(filename)
                filename = f"{stem.stem}-{used_names[filename]}{stem.suffix}"
            else:
                used_names[filename] = 0
            result = self.download_attachment(content_id, attachment_id, output_dir / filename)
            saved.append(result)
        return saved

    def parse_response(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            self._raise_http_error(response)
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    def _raise_http_error(self, response: httpx.Response) -> None:
        details: Any = None
        message = response.reason_phrase or "HTTP error"
        try:
            details = response.json()
            if isinstance(details, dict):
                message = details.get("message") or details.get("error") or message
        except Exception:
            details = response.text[:500] if response.text else None

        category = ErrorCategory.CLIENT if response.status_code < 500 else ErrorCategory.SERVER
        make_error(
            code=f"HTTP_{response.status_code}",
            category=category,
            message=str(message),
            http_status=response.status_code,
            details=details,
            command=get_ctx().command,
        )

    def get_content_version(self, content_id: str) -> int:
        data = self.parse_response(
            self.request(
                "GET",
                f"/rest/api/content/{content_id}",
                params={"expand": "version"},
            )
        )
        if not isinstance(data, dict) or "version" not in data:
            make_error(
                code="VERSION_NOT_FOUND",
                category=ErrorCategory.CLIENT,
                message=f"Cannot read version for content {content_id}.",
                hint="Verify content id exists.",
                command=get_ctx().command,
            )
        return int(data["version"]["number"])

    def paginate(
        self,
        path: str,
        params: dict[str, Any],
        *,
        limit: int,
        start: int = 0,
        fetch_all: bool = False,
    ) -> Iterator[dict[str, Any]]:
        current_start = start
        page_limit = limit
        while True:
            page_params = {**params, "limit": str(page_limit), "start": str(current_start)}
            response = self.request("GET", path, params=page_params)
            data = self.parse_response(response)
            if not isinstance(data, dict):
                yield {"_raw": data}
                return

            results = data.get("results", [])
            if isinstance(results, list):
                for item in results:
                    if isinstance(item, dict):
                        yield item

            if not fetch_all:
                return

            size = data.get("size", len(results))
            if size < page_limit:
                return
            current_start += page_limit
            if "_links" in data and not data.get("results"):
                return


def get_client() -> ConfluenceClient:
    pat, api_url = load_config()
    return ConfluenceClient(pat, api_url, get_ctx().timeout)


def run_api(
    handler: Callable[[ConfluenceClient], Any],
    *,
    command: str,
) -> None:
    get_ctx().command = command
    start = time.monotonic()
    client = get_client()
    try:
        data = handler(client)
        duration_ms = int((time.monotonic() - start) * 1000)
        emit(make_ok(data, command=command, duration_ms=duration_ms))
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Swagger loader
# ---------------------------------------------------------------------------

_swagger_cache: Optional[dict[str, Any]] = None


def load_swagger() -> dict[str, Any]:
    global _swagger_cache
    if _swagger_cache is None:
        _swagger_cache = json.loads(SWAGGER_PATH.read_text(encoding="utf-8"))
    return _swagger_cache


def list_openapi_operations(*, tag: Optional[str] = None, limit: Optional[int] = None) -> list[dict[str, Any]]:
    swagger = load_swagger()
    ops: list[dict[str, Any]] = []
    for path, methods in swagger.get("paths", {}).items():
        for method, spec in methods.items():
            if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            tags = spec.get("tags", [])
            if tag and tag not in tags:
                continue
            ops.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "operationId": spec.get("operationId"),
                    "summary": spec.get("summary"),
                    "tags": tags,
                }
            )
    ops.sort(key=lambda o: (o["path"], o["method"]))
    if limit is not None:
        ops = ops[:limit]
    return ops


def describe_openapi_operation(operation_id: str) -> dict[str, Any]:
    swagger = load_swagger()
    for path, methods in swagger.get("paths", {}).items():
        for method, spec in methods.items():
            if spec.get("operationId") == operation_id:
                return {
                    "method": method.upper(),
                    "path": path,
                    "operationId": operation_id,
                    "summary": spec.get("summary"),
                    "tags": spec.get("tags", []),
                    "parameters": spec.get("parameters", []),
                    "requestBody": spec.get("requestBody"),
                    "responses": spec.get("responses", {}),
                }
    make_error(
        code="OPERATION_NOT_FOUND",
        category=ErrorCategory.VALIDATION,
        message=f"operationId not found: {operation_id}",
        hint="Run: openapi list --json",
        command=get_ctx().command,
        exit_code=EXIT_USAGE,
    )


# ---------------------------------------------------------------------------
# TOOL_REGISTRY
# ---------------------------------------------------------------------------


TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "confluence_schema",
        "description": "Get CLI tool definitions for AI agents",
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["default", "openai", "anthropic"], "default": "default"},
            },
        },
    },
    {
        "name": "confluence_openapi_list",
        "description": "List Confluence REST API operations from bundled swagger",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "confluence_openapi_describe",
        "description": "Describe a Confluence REST API operation by operationId",
        "input_schema": {
            "type": "object",
            "properties": {"operation_id": {"type": "string"}},
            "required": ["operation_id"],
        },
    },
    {
        "name": "confluence_request",
        "description": "Generic HTTP request to any Confluence REST endpoint",
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                "path": {"type": "string"},
                "query": {"type": "object", "additionalProperties": {"type": "string"}},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["method", "path"],
        },
    },
    {
        "name": "confluence_server_info",
        "description": "Get Confluence server information",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "confluence_search_cql",
        "description": "Search Confluence content using CQL",
        "input_schema": {
            "type": "object",
            "properties": {
                "cql": {"type": "string"},
                "limit": {"type": "integer", "default": 25},
                "start": {"type": "integer", "default": 0},
                "expand": {"type": "string"},
                "all": {"type": "boolean"},
                "stream": {"type": "boolean"},
                "fields": {"type": "string"},
            },
            "required": ["cql"],
        },
    },
    {
        "name": "confluence_content_list",
        "description": "List Confluence content",
        "input_schema": {
            "type": "object",
            "properties": {
                "space_key": {"type": "string"},
                "type": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
                "expand": {"type": "string"},
                "all": {"type": "boolean"},
                "fields": {"type": "string"},
            },
        },
    },
    {
        "name": "confluence_content_get",
        "description": "Get Confluence content by ID",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "expand": {"type": "string", "default": "body.storage,version,space"},
                "version": {"type": "string"},
                "status": {"type": "string"},
                "fields": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "confluence_content_resolve_url",
        "description": "Extract Confluence page ID from a web URL or numeric ID",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "confluence_content_export",
        "description": "Export page metadata, body HTML, and all attachments to a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "output_dir": {
                    "type": "string",
                    "description": "Export directory (default: scripts/output/scex-confluence/<CR-XXX-id>).",
                },
                "expand": {"type": "string", "default": "body.storage,version,space"},
                "skip_attachments": {"type": "boolean", "default": False},
            },
            "required": ["id"],
        },
    },
    {
        "name": "confluence_content_create",
        "description": "Create Confluence content (page/blogpost)",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "space_key": {"type": "string"},
                "body_storage": {"type": "string"},
                "type": {"type": "string", "default": "page"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
                "expand": {"type": "string"},
            },
        },
    },
    {
        "name": "confluence_content_update",
        "description": "Update Confluence content; use auto_version to avoid 409 conflicts",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "auto_version": {"type": "boolean"},
                "title": {"type": "string"},
                "body_storage": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
                "expand": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "confluence_content_delete",
        "description": "Delete Confluence content (requires force)",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "status": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "confluence_space_list",
        "description": "List Confluence spaces",
        "input_schema": {
            "type": "object",
            "properties": {
                "space_key": {"type": "string"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
                "expand": {"type": "string"},
                "all": {"type": "boolean"},
                "fields": {"type": "string"},
            },
        },
    },
    {
        "name": "confluence_space_get",
        "description": "Get a Confluence space by key",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "expand": {"type": "string"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "confluence_space_create",
        "description": "Create a Confluence space",
        "input_schema": {
            "type": "object",
            "properties": {
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
        },
    },
    {
        "name": "confluence_space_update",
        "description": "Update a Confluence space",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "confluence_space_delete",
        "description": "Delete a Confluence space (requires force)",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "force": {"type": "boolean"}},
            "required": ["key"],
        },
    },
    {
        "name": "confluence_attachment_list",
        "description": "List attachments for content",
        "input_schema": {
            "type": "object",
            "properties": {"content_id": {"type": "string"}, "expand": {"type": "string"}},
            "required": ["content_id"],
        },
    },
    {
        "name": "confluence_attachment_upload",
        "description": "Upload attachment to content",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
                "file": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["content_id", "file"],
        },
    },
    {
        "name": "confluence_attachment_download",
        "description": "Download attachment binary to file",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "output": {"type": "string"},
            },
            "required": ["content_id", "attachment_id", "output"],
        },
    },
    {
        "name": "confluence_attachment_download_all",
        "description": "Download all attachments for content into a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "content_id": {"type": "string"},
                "output_dir": {
                    "type": "string",
                    "description": "Download directory (default: scripts/output/scex-confluence/<CR-XXX-id>).",
                },
            },
            "required": ["content_id"],
        },
    },
    {
        "name": "confluence_user_current",
        "description": "Get current Confluence user",
        "input_schema": {"type": "object", "properties": {"expand": {"type": "string"}}},
    },
    {
        "name": "confluence_user_list",
        "description": "List Confluence users",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "start": {"type": "integer"}},
        },
    },
    {
        "name": "confluence_label_recent",
        "description": "Get recently used labels",
        "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
    },
    {
        "name": "confluence_label_related",
        "description": "Get labels related to a label name",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "confluence_convert_body",
        "description": "Convert content body representation",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
            "required": ["to"],
        },
    },
]


def export_schema(format_name: str) -> Any:
    envelope = CliResult.model_json_schema()
    if format_name == "openai":
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in TOOL_REGISTRY
        ]
    if format_name == "anthropic":
        return [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in TOOL_REGISTRY
        ]
    return {
        "tools": TOOL_REGISTRY,
        "response_envelope": envelope,
        "stream_format": {
            "description": "NDJSON events: progress | item | done | error",
            "note": "When --stream is set, CliResult envelope is bypassed; last line is event=done.",
        },
    }


# ---------------------------------------------------------------------------
# Streaming list helper
# ---------------------------------------------------------------------------


def stream_paginated(
    client: ConfluenceClient,
    path: str,
    params: dict[str, Any],
    *,
    command: str,
    limit: int,
    start: int,
    fetch_all: bool,
    fields: Optional[str],
) -> None:
    start_time = time.monotonic()
    count = 0
    page = 0
    current_start = start
    page_limit = limit

    while True:
        page += 1
        page_params = {**params, "limit": str(page_limit), "start": str(current_start)}
        response = client.request("GET", path, params=page_params)
        data = client.parse_response(response)
        if not isinstance(data, dict):
            emit_stream_event({"event": "item", "data": data})
            count += 1
            break

        results = data.get("results", [])
        if isinstance(results, list):
            for item in results:
                emit_stream_event({"event": "item", "data": project_fields(item, fields)})
                count += 1

        emit_stream_event({"event": "progress", "data": {"page": page, "fetched": count}})

        if not fetch_all:
            break
        size = data.get("size", len(results) if isinstance(results, list) else 0)
        if not isinstance(results, list) or size < page_limit:
            break
        current_start += page_limit

    duration_ms = int((time.monotonic() - start_time) * 1000)
    emit_stream_done(command=command, count=count, duration_ms=duration_ms)


def require_force(command: str, *, destructive: bool = True) -> None:
    if destructive and not get_ctx().force:
        make_error(
            code="FORCE_REQUIRED",
            category=ErrorCategory.VALIDATION,
            message="Destructive operation requires --force.",
            hint=f"Re-run with --force: {command}",
            command=command,
            exit_code=EXIT_USAGE,
        )


# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------


class OrderedTyperGroup(TyperGroup):
    def list_commands(self, ctx: typer.Context) -> list[str]:
        return sorted(self.commands.keys())


app = typer.Typer(
    name="scex-confluence",
    help="AI-friendly CLI for Confluence Data Center REST API.",
    no_args_is_help=True,
    cls=OrderedTyperGroup,
)


@app.callback()
def global_options_callback(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", help="Output CliResult JSON envelope."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress non-essential output."),
    stream: bool = typer.Option(False, "--stream", help="NDJSON stream; bypasses envelope."),
    pat: Optional[str] = typer.Option(None, "--pat", help="Personal Access Token."),
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Confluence base URL."),
    timeout: float = typer.Option(DEFAULT_TIMEOUT, "--timeout", help="HTTP timeout in seconds."),
    force: bool = typer.Option(False, "--force", help="Skip destructive operation warnings."),
) -> None:
    _ctx.json_output = json_output
    _ctx.quiet = quiet
    _ctx.stream = stream
    _ctx.pat = pat
    _ctx.api_url = api_url
    _ctx.timeout = timeout
    _ctx.force = force


# --- schema ---

schema_app = typer.Typer(help="Export tool definitions for AI agents.")
app.add_typer(schema_app, name="schema")


@schema_app.callback(invoke_without_command=True)
def schema_cmd(
    ctx: typer.Context,
    format_name: str = typer.Option("default", "--format", help="default|openai|anthropic"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    get_ctx().command = "schema"
    data = export_schema(format_name)
    emit(make_ok(data, command="schema"))


# --- openapi ---

openapi_app = typer.Typer(help="Browse bundled OpenAPI swagger.")
app.add_typer(openapi_app, name="openapi")


@openapi_app.command("list")
def openapi_list(
    tag: Optional[str] = typer.Option(None, "--tag"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    get_ctx().command = "openapi list"
    ops = list_openapi_operations(tag=tag, limit=limit)
    emit(make_ok({"count": len(ops), "operations": ops}, command="openapi list"))


@openapi_app.command("describe")
def openapi_describe(operation_id: str = typer.Argument(..., help="operationId from swagger")) -> None:
    get_ctx().command = "openapi describe"
    emit(make_ok(describe_openapi_operation(operation_id), command="openapi describe"))


# --- request ---

@app.command("request")
def request_cmd(
    method: str = typer.Argument(..., help="HTTP method"),
    path: str = typer.Argument(..., help="API path e.g. /rest/api/content/123"),
    query: Optional[list[str]] = typer.Option(None, "--query", help="key=value (repeatable)"),
    header: Optional[list[str]] = typer.Option(None, "--header", help="Header: value (repeatable)"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json or path"),
    output_file: Optional[Path] = typer.Option(None, "--output-file"),
) -> None:
    command = f"request {method.upper()} {path}"
    get_ctx().command = command

    if method.upper() == "DELETE" and not get_ctx().force:
        require_force(command)

    params: dict[str, str] = {}
    if query:
        for item in query:
            if "=" not in item:
                make_error(
                    code="INVALID_QUERY",
                    category=ErrorCategory.VALIDATION,
                    message=f"Query must be key=value: {item}",
                    command=command,
                    exit_code=EXIT_USAGE,
                )
            key, value = item.split("=", 1)
            params[key] = value

    headers: dict[str, str] = {}
    if header:
        for item in header:
            if ":" not in item:
                make_error(
                    code="INVALID_HEADER",
                    category=ErrorCategory.VALIDATION,
                    message=f"Header must be Name: value: {item}",
                    command=command,
                    exit_code=EXIT_USAGE,
                )
            name, value = item.split(":", 1)
            headers[name.strip()] = value.strip()

    json_body = resolve_body(
        body_json=body_json,
        body_stdin=body_stdin,
        body_file=body_file,
        command=command,
    )

    def handler(client: ConfluenceClient) -> Any:
        if is_site_path(path):
            response = client.request_site(
                method,
                path,
                params=params or None,
                headers=headers or None,
            )
        else:
            response = client.request(
                method,
                path,
                params=params or None,
                json_body=json_body,
                headers=headers or None,
            )
        if output_file is not None:
            client.save_binary(response, output_file)
            return {"saved": str(output_file), "http_status": response.status_code, "bytes": len(response.content)}
        return client.parse_response(response)

    run_api(handler, command=command)


# --- server ---

server_app = typer.Typer(help="Server information.")
app.add_typer(server_app, name="server")


@server_app.command("info")
def server_info() -> None:
    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(client.request("GET", "/rest/api/server-information"))

    run_api(handler, command="server info")


# --- search ---

search_app = typer.Typer(help="Search Confluence via CQL.")
app.add_typer(search_app, name="search")


@search_app.command("cql")
def search_cql(
    cql: str = typer.Argument(..., help="CQL query string"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    expand: Optional[str] = typer.Option(None, "--expand"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "search cql"
    get_ctx().command = command
    params: dict[str, Any] = {"cql": cql}
    if expand:
        params["expand"] = expand

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            stream_paginated(
                client,
                "/rest/api/content/search",
                params,
                command=command,
                limit=limit,
                start=start,
                fetch_all=fetch_all,
                fields=fields,
            )
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: ConfluenceClient) -> Any:
        params["limit"] = str(limit)
        params["start"] = str(start)
        data = client.parse_response(client.request("GET", "/rest/api/content/search", params=params))
        return project_fields(data, fields)

    run_api(handler, command=command)


# --- content ---

content_app = typer.Typer(help="Content operations.")
app.add_typer(content_app, name="content")


@content_app.command("list")
def content_list(
    space_key: Optional[str] = typer.Option(None, "--space-key"),
    content_type: Optional[str] = typer.Option(None, "--type"),
    title: Optional[str] = typer.Option(None, "--title"),
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    expand: Optional[str] = typer.Option(None, "--expand"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "content list"
    get_ctx().command = command
    params: dict[str, Any] = {}
    if space_key:
        params["spaceKey"] = space_key
    if content_type:
        params["type"] = content_type
    if title:
        params["title"] = title
    if status:
        params["status"] = status
    if expand:
        params["expand"] = expand

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            stream_paginated(
                client,
                "/rest/api/content",
                params,
                command=command,
                limit=limit,
                start=start,
                fetch_all=fetch_all,
                fields=fields,
            )
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: ConfluenceClient) -> Any:
        params["limit"] = str(limit)
        params["start"] = str(start)
        data = client.parse_response(client.request("GET", "/rest/api/content", params=params))
        return project_fields(data, fields)

    run_api(handler, command=command)


@content_app.command("get")
def content_get(
    content_id: str = typer.Argument(...),
    expand: str = typer.Option("body.storage,version,space", "--expand"),
    version: Optional[str] = typer.Option(None, "--version"),
    status: Optional[str] = typer.Option(None, "--status"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        params: dict[str, str] = {"expand": expand}
        if version:
            params["version"] = version
        if status:
            params["status"] = status
        data = client.parse_response(
            client.request("GET", f"/rest/api/content/{content_id}", params=params)
        )
        return project_fields(data, fields)

    run_api(handler, command="content get")


@content_app.command("resolve-url")
def content_resolve_url(url: str = typer.Argument(..., help="Confluence page URL or numeric page ID")) -> None:
    command = "content resolve-url"
    page_id = parse_confluence_page_id(url, command=command)

    def handler(client: ConfluenceClient) -> Any:
        _ = client
        return {"id": page_id, "input": url}

    run_api(handler, command=command)


def export_content_to_dir(
    client: ConfluenceClient,
    content_id: str,
    output_dir: Path | None,
    *,
    expand: str,
    skip_attachments: bool,
) -> dict[str, Any]:
    page = client.parse_response(
        client.request("GET", f"/rest/api/content/{content_id}", params={"expand": expand})
    )
    if not isinstance(page, dict):
        make_error(
            code="CONTENT_NOT_FOUND",
            category=ErrorCategory.CLIENT,
            message=f"Content {content_id} not found.",
            command=get_ctx().command,
        )

    title = page.get("title") if isinstance(page.get("title"), str) else None
    resolved_output_dir = resolve_export_output_dir(content_id, output_dir=output_dir, title=title)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    page_path = resolved_output_dir / "page.json"
    page_path.write_text(json.dumps(page, ensure_ascii=False, indent=2), encoding="utf-8")

    body_html = ""
    storage = page.get("body", {}).get("storage", {})
    if isinstance(storage, dict):
        body_html = str(storage.get("value", ""))
    (resolved_output_dir / "body.html").write_text(body_html, encoding="utf-8")

    attachments_meta = client.list_attachments(content_id, expand="version,container")
    (resolved_output_dir / "attachments.json").write_text(
        json.dumps({"results": attachments_meta}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    downloaded: list[dict[str, Any]] = []
    if not skip_attachments and attachments_meta:
        downloaded = client.download_all_attachments(content_id, resolved_output_dir)

    manifest = {
        "id": page.get("id", content_id),
        "title": page.get("title"),
        "space": page.get("space", {}).get("key") if isinstance(page.get("space"), dict) else None,
        "version": page.get("version", {}).get("number") if isinstance(page.get("version"), dict) else None,
        "output_dir": str(resolved_output_dir),
        "files": {
            "page": str(page_path.name),
            "body": "body.html",
            "attachments_index": "attachments.json",
            "manifest": "manifest.json",
        },
        "attachments": downloaded,
    }
    (resolved_output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


@content_app.command("export")
def content_export(
    content_id: str = typer.Argument(...),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Export directory (default: scripts/output/scex-confluence/<CR-XXX-id>).",
    ),
    expand: str = typer.Option("body.storage,version,space", "--expand"),
    skip_attachments: bool = typer.Option(False, "--skip-attachments"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        return export_content_to_dir(
            client,
            content_id,
            output_dir,
            expand=expand,
            skip_attachments=skip_attachments,
        )

    run_api(handler, command="content export")


@content_app.command("create")
def content_create(
    title: Optional[str] = typer.Option(None, "--title"),
    space_key: Optional[str] = typer.Option(None, "--space-key"),
    body_storage: Optional[str] = typer.Option(None, "--body-storage"),
    content_type: str = typer.Option("page", "--type"),
    expand: Optional[str] = typer.Option(None, "--expand"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body"),
) -> None:
    command = "content create"
    get_ctx().command = command
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is None:
        payload = build_content_body(
            title=title,
            space_key=space_key,
            body_storage=body_storage,
            content_type=content_type,
        )
    elif title or space_key or body_storage:
        payload = build_content_body(
            title=title,
            space_key=space_key,
            body_storage=body_storage,
            content_type=content_type,
            existing=payload if isinstance(payload, dict) else None,
        )

    params: dict[str, str] = {}
    if expand:
        params["expand"] = expand

    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(
            client.request("POST", "/rest/api/content", params=params or None, json_body=payload)
        )

    run_api(handler, command=command)


@content_app.command("update")
def content_update(
    content_id: str = typer.Argument(...),
    auto_version: bool = typer.Option(False, "--auto-version"),
    title: Optional[str] = typer.Option(None, "--title"),
    body_storage: Optional[str] = typer.Option(None, "--body-storage"),
    expand: Optional[str] = typer.Option(None, "--expand"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body"),
) -> None:
    command = "content update"
    get_ctx().command = command
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is None:
        payload = build_content_body(title=title, body_storage=body_storage, content_type="")
    elif title or body_storage:
        payload = build_content_body(
            title=title,
            body_storage=body_storage,
            content_type="",
            existing=payload if isinstance(payload, dict) else None,
        )

    if not isinstance(payload, dict):
        make_error(
            code="INVALID_BODY",
            category=ErrorCategory.VALIDATION,
            message="Update body must be a JSON object.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    payload["id"] = content_id
    payload["type"] = payload.get("type") or "page"

    if auto_version:
        pass  # handled in handler
    elif "version" not in payload or "number" not in payload.get("version", {}):
        make_error(
            code="MISSING_VERSION",
            category=ErrorCategory.VALIDATION,
            message="Content update requires version.number or --auto-version.",
            hint="Use --auto-version to fetch and increment version automatically.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    params: dict[str, str] = {}
    if expand:
        params["expand"] = expand

    def handler(client: ConfluenceClient) -> Any:
        body = dict(payload)
        if auto_version:
            current = client.get_content_version(content_id)
            body["version"] = {"number": current + 1}
        return client.parse_response(
            client.request("PUT", f"/rest/api/content/{content_id}", params=params or None, json_body=body)
        )

    run_api(handler, command=command)


@content_app.command("delete")
def content_delete(
    content_id: str = typer.Argument(...),
    status: Optional[str] = typer.Option(None, "--status"),
) -> None:
    require_force("content delete")
    params: dict[str, str] = {}
    if status:
        params["status"] = status

    def handler(client: ConfluenceClient) -> Any:
        response = client.request("DELETE", f"/rest/api/content/{content_id}", params=params or None)
        return client.parse_response(response)

    run_api(handler, command="content delete")


# --- space ---

space_app = typer.Typer(help="Space operations.")
app.add_typer(space_app, name="space")


@space_app.command("list")
def space_list(
    space_key: Optional[str] = typer.Option(None, "--space-key"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    expand: Optional[str] = typer.Option(None, "--expand"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "space list"
    get_ctx().command = command
    params: dict[str, Any] = {}
    if space_key:
        params["spaceKey"] = space_key
    if expand:
        params["expand"] = expand

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            stream_paginated(
                client,
                "/rest/api/space",
                params,
                command=command,
                limit=limit,
                start=start,
                fetch_all=fetch_all,
                fields=fields,
            )
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: ConfluenceClient) -> Any:
        params["limit"] = str(limit)
        params["start"] = str(start)
        data = client.parse_response(client.request("GET", "/rest/api/space", params=params))
        return project_fields(data, fields)

    run_api(handler, command=command)


@space_app.command("get")
def space_get(
    key: str = typer.Argument(...),
    expand: Optional[str] = typer.Option(None, "--expand"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        params: dict[str, str] = {}
        if expand:
            params["expand"] = expand
        data = client.parse_response(client.request("GET", f"/rest/api/space/{key}", params=params or None))
        return project_fields(data, fields)

    run_api(handler, command=f"space get {key}")


@space_app.command("create")
def space_create(
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body"),
) -> None:
    command = "space create"
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is None:
        make_error(
            code="MISSING_BODY",
            category=ErrorCategory.VALIDATION,
            message="space create requires --body-json, --body-stdin, or --body.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(client.request("POST", "/rest/api/space", json_body=payload))

    run_api(handler, command=command)


@space_app.command("update")
def space_update(
    key: str = typer.Argument(...),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body"),
) -> None:
    command = "space update"
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is None:
        make_error(
            code="MISSING_BODY",
            category=ErrorCategory.VALIDATION,
            message="space update requires --body-json, --body-stdin, or --body.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(client.request("PUT", f"/rest/api/space/{key}", json_body=payload))

    run_api(handler, command=f"space update {key}")


@space_app.command("delete")
def space_delete(key: str = typer.Argument(...)) -> None:
    require_force(f"space delete {key}")

    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(client.request("DELETE", f"/rest/api/space/{key}"))

    run_api(handler, command=f"space delete {key}")


# --- attachment ---

attachment_app = typer.Typer(help="Attachment operations.")
app.add_typer(attachment_app, name="attachment")


@attachment_app.command("list")
def attachment_list(
    content_id: str = typer.Argument(...),
    expand: Optional[str] = typer.Option(None, "--expand"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        params: dict[str, str] = {}
        if expand:
            params["expand"] = expand
        data = client.parse_response(
            client.request("GET", f"/rest/api/content/{content_id}/child/attachment", params=params or None)
        )
        return project_fields(data, fields)

    run_api(handler, command="attachment list")


@attachment_app.command("upload")
def attachment_upload(
    content_id: str = typer.Argument(...),
    file: Path = typer.Option(..., "--file", exists=True, readable=True),
    comment: Optional[str] = typer.Option(None, "--comment"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        headers = {"X-Atlassian-Token": "no-check"}
        files = {"file": (file.name, file.read_bytes())}
        data: dict[str, str] = {}
        if comment:
            data["comment"] = comment
        response = client.request(
            "POST",
            f"/rest/api/content/{content_id}/child/attachment",
            headers=headers,
            files=files,
            params=data or None,
        )
        return client.parse_response(response)

    run_api(handler, command="attachment upload")


@attachment_app.command("download")
def attachment_download(
    content_id: str = typer.Argument(...),
    attachment_id: str = typer.Argument(...),
    output: Path = typer.Option(..., "--output"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        return client.download_attachment(content_id, attachment_id, output)

    run_api(handler, command="attachment download")


@attachment_app.command("download-all")
def attachment_download_all(
    content_id: str = typer.Argument(...),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Download directory (default: scripts/output/scex-confluence/<CR-XXX-id>).",
    ),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        title: str | None = None
        page = client.parse_response(
            client.request("GET", f"/rest/api/content/{content_id}", params={"fields": "title"})
        )
        if isinstance(page, dict) and isinstance(page.get("title"), str):
            title = page["title"]
        resolved_output_dir = resolve_export_output_dir(content_id, output_dir=output_dir, title=title)
        saved = client.download_all_attachments(content_id, resolved_output_dir)
        return {"output_dir": str(resolved_output_dir), "count": len(saved), "attachments": saved}

    run_api(handler, command="attachment download-all")


# --- user ---

user_app = typer.Typer(help="User operations.")
app.add_typer(user_app, name="user")


@user_app.command("current")
def user_current(expand: Optional[str] = typer.Option(None, "--expand")) -> None:
    def handler(client: ConfluenceClient) -> Any:
        params: dict[str, str] = {}
        if expand:
            params["expand"] = expand
        return client.parse_response(client.request("GET", "/rest/api/user/current", params=params or None))

    run_api(handler, command="user current")


@user_app.command("list")
def user_list(
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
) -> None:
    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(
            client.request("GET", "/rest/api/user/list", params={"limit": str(limit), "start": str(start)})
        )

    run_api(handler, command="user list")


# --- label ---

label_app = typer.Typer(help="Label operations.")
app.add_typer(label_app, name="label")


@label_app.command("recent")
def label_recent(limit: int = typer.Option(DEFAULT_LIMIT, "--limit")) -> None:
    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(
            client.request("GET", "/rest/api/label/recent", params={"limit": str(limit)})
        )

    run_api(handler, command="label recent")


@label_app.command("related")
def label_related(name: str = typer.Argument(...)) -> None:
    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(client.request("GET", f"/rest/api/label/{name}/related"))

    run_api(handler, command="label related")


# --- convert ---

convert_app = typer.Typer(help="Content body conversion.")
app.add_typer(convert_app, name="convert")


@convert_app.command("body")
def convert_body(
    to: str = typer.Option(..., "--to"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body"),
) -> None:
    command = "convert body"
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is None:
        make_error(
            code="MISSING_BODY",
            category=ErrorCategory.VALIDATION,
            message="convert body requires --body-json, --body-stdin, or --body.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    def handler(client: ConfluenceClient) -> Any:
        return client.parse_response(
            client.request("POST", f"/rest/api/contentbody/convert/{to}", json_body=payload)
        )

    run_api(handler, command=command)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
