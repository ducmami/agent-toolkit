#!/usr/bin/env python3
"""AI-friendly CLI for Jira Data Center REST API."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import typer
from dotenv import load_dotenv
from pydantic import BaseModel
from typer.core import TyperGroup

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PLATFORM_SWAGGER_PATH = SCRIPT_DIR / "platform-swagger-v3.v3.json"
SOFTWARE_SWAGGER_PATH = SCRIPT_DIR / "software-swagger.v3.json"
DEFAULT_EXPORT_OUTPUT_BASE = SCRIPT_DIR.parent / "output" / "scex-jira"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFIG = 3

DEFAULT_LIMIT = 50
DEFAULT_TIMEOUT = 30.0
DEFAULT_ISSUE_EXPAND = "renderedFields,names,changelog"
DEFAULT_SEARCH_EXPAND = "renderedFields"
AGILE_PREFIX = "/rest/agile/1.0"

ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$", re.IGNORECASE)
ISSUE_KEY_FIND_RE = re.compile(r"([A-Z][A-Z0-9_]+-\d+)", re.IGNORECASE)


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
    api_version: str = "2"
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
    return None


def parse_csv_list(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


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

    list_keys = ("results", "issues", "comments", "values", "transitions")
    if isinstance(data, dict):
        for list_key in list_keys:
            items = data.get(list_key)
            if isinstance(items, list):
                projected = dict(data)
                projected[list_key] = [pick(item) if isinstance(item, dict) else item for item in items]
                return projected
        return pick(data)
    if isinstance(data, list):
        return [pick(item) if isinstance(item, dict) else item for item in data]
    return data


# ---------------------------------------------------------------------------
# Config / URL helpers
# ---------------------------------------------------------------------------


def normalize_api_url(api_url: str) -> str:
    return api_url.rstrip("/")


def site_root(api_url: str) -> str:
    root = normalize_api_url(api_url)
    for suffix in ("/rest/api/3", "/rest/api/2", "/rest/api", "/rest"):
        if root.endswith(suffix):
            return root[: -len(suffix)]
    return root


SITE_PATH_PREFIXES = ("/secure/", "/browse/", "/s/", "/plugins/", "/images/", "/download/")


def is_site_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in SITE_PATH_PREFIXES)


def rest_prefix(api_version: str) -> str:
    return f"/rest/api/{api_version}"


def normalize_api_path(path: str, *, api_version: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    if is_site_path(path) or path.startswith("/rest/"):
        return path
    return f"{rest_prefix(api_version)}{path}"


def parse_issue_key(url_or_key: str, *, command: str) -> str:
    text = url_or_key.strip()
    if not text:
        make_error(
            code="INVALID_ISSUE_KEY",
            category=ErrorCategory.VALIDATION,
            message="Issue key or browse URL is empty.",
            hint="Pass ITSD-133 or https://jira.example.com/browse/ITSD-133.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    if text.isdigit():
        return text
    if ISSUE_KEY_RE.match(text):
        return text.upper()

    parsed = urlparse(text)
    query = parse_qs(parsed.query)
    selected = query.get("selectedIssue") or query.get("issueKey")
    if selected and selected[0]:
        candidate = selected[0].strip()
        if ISSUE_KEY_RE.match(candidate) or candidate.isdigit():
            return candidate.upper() if not candidate.isdigit() else candidate

    path = parsed.path or text
    for pattern in (r"/browse/([A-Z][A-Z0-9_]+-\d+)", r"/issues/([A-Z][A-Z0-9_]+-\d+)"):
        match = re.search(pattern, path, re.IGNORECASE)
        if match:
            return match.group(1).upper()

    found = ISSUE_KEY_FIND_RE.search(text)
    if found:
        return found.group(1).upper()

    make_error(
        code="INVALID_ISSUE_KEY",
        category=ErrorCategory.VALIDATION,
        message=f"Cannot extract issue key from: {url_or_key}",
        hint="Pass an issue key (ITSD-133), numeric id, or a /browse/<KEY> URL.",
        command=command,
        exit_code=EXIT_USAGE,
    )
    raise AssertionError("unreachable")


def sanitize_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "attachment"


def resolve_export_output_dir(issue_key: str, *, output_dir: Path | None = None) -> Path:
    if output_dir is not None:
        return output_dir
    return DEFAULT_EXPORT_OUTPUT_BASE / sanitize_filename(issue_key)


def description_parts(issue: dict[str, Any]) -> tuple[str, str]:
    rendered = issue.get("renderedFields") if isinstance(issue.get("renderedFields"), dict) else {}
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    html = rendered.get("description") if isinstance(rendered, dict) else None
    raw = fields.get("description") if isinstance(fields, dict) else None
    wiki = ""
    if isinstance(raw, str):
        wiki = raw
    elif raw is not None:
        wiki = json.dumps(raw, ensure_ascii=False, indent=2)
    html_text = html if isinstance(html, str) and html else wiki
    return html_text, wiki


def load_config() -> tuple[str, str]:
    load_dotenv(REPO_ROOT / ".env")
    ctx = get_ctx()
    pat = ctx.pat or os.environ.get("SCEX_JIRA_PAT")
    api_url = ctx.api_url or os.environ.get("SCEX_JIRA_API_URL")
    if not pat:
        make_error(
            code="MISSING_PAT",
            category=ErrorCategory.CONFIG,
            message="Missing Personal Access Token.",
            hint="Set SCEX_JIRA_PAT in .env or pass --pat <token>.",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    if not api_url:
        make_error(
            code="MISSING_API_URL",
            category=ErrorCategory.CONFIG,
            message="Missing Jira API URL.",
            hint="Set SCEX_JIRA_API_URL in .env or pass --api-url <url>.",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    api_url = api_url.rstrip("/")
    if not api_url.startswith(("http://", "https://")):
        make_error(
            code="INVALID_API_URL",
            category=ErrorCategory.CONFIG,
            message=f"API URL must include scheme: {api_url}",
            hint="Example: https://jira.example.com",
            command=ctx.command or "config",
            exit_code=EXIT_CONFIG,
        )
    return pat, api_url


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class JiraClient:
    def __init__(self, pat: str, api_url: str, timeout: float, api_version: str) -> None:
        self.api_url = normalize_api_url(api_url)
        self.site_url = site_root(self.api_url)
        self.api_version = api_version
        self._pat = pat
        self._timeout = timeout
        auth_headers = {"Authorization": f"Bearer {pat}"}
        self._client = httpx.Client(
            base_url=self.site_url,
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

    def rest_path(self, path: str) -> str:
        return normalize_api_path(path, api_version=self.api_version)

    def agile_path(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if path.startswith("/rest/agile"):
            return path
        return f"{AGILE_PREFIX}{path}"

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
        path = self.rest_path(path) if not path.startswith("/rest/agile") else self.agile_path(path)
        return self._send(
            self._client,
            method,
            path,
            params=params,
            json_body=json_body,
            headers=headers,
            files=files,
            content=content,
        )

    def request_agile(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[Any] = None,
    ) -> httpx.Response:
        return self._send(self._client, method, self.agile_path(path), params=params, json_body=json_body)

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
        raise AssertionError("unreachable")

    def save_binary(self, response: httpx.Response, output: Path) -> None:
        if response.status_code >= 400:
            self._raise_http_error(response)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type.lower():
            self._reject_html_login(response, decode_body=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(response.content)

    def parse_response(self, response: httpx.Response) -> Any:
        if response.status_code >= 400:
            self._raise_http_error(response)
        if response.status_code == 204 or not response.content:
            return None
        self._reject_html_login(response, decode_body=True)
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            return response.json()
        return response.text

    def _reject_html_login(self, response: httpx.Response, *, decode_body: bool) -> None:
        content_type = response.headers.get("content-type", "")
        text_preview = ""
        if decode_body:
            try:
                text_preview = response.text[:2000]
            except Exception:
                text_preview = ""
        looks_like_html = "text/html" in content_type.lower()
        if decode_body and not looks_like_html:
            stripped = text_preview.lstrip().lower()
            looks_like_html = stripped.startswith("<!doctype html") or stripped.startswith("<html")
        if not looks_like_html:
            return
        login_page = "login" in text_preview.lower() or "os_username" in text_preview
        make_error(
            code="AUTH_FAILED" if login_page else "UNEXPECTED_HTML",
            category=ErrorCategory.CONFIG if login_page else ErrorCategory.CLIENT,
            message="Jira returned an HTML login page instead of JSON." if login_page else "Jira returned HTML instead of JSON.",
            hint="Check SCEX_JIRA_PAT / SCEX_JIRA_API_URL. API URL should be the site origin (https://jira.example.com), with or without /rest.",
            http_status=response.status_code,
            command=get_ctx().command,
            exit_code=EXIT_CONFIG if login_page else EXIT_ERROR,
        )

    def _raise_http_error(self, response: httpx.Response) -> None:
        details: Any = None
        message = response.reason_phrase or "HTTP error"
        try:
            details = response.json()
            if isinstance(details, dict):
                error_messages = details.get("errorMessages")
                errors = details.get("errors")
                if isinstance(error_messages, list) and error_messages:
                    message = "; ".join(str(item) for item in error_messages)
                elif isinstance(errors, dict) and errors:
                    message = "; ".join(f"{key}: {value}" for key, value in errors.items())
                else:
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

    def get_issue(
        self,
        issue_key: str,
        *,
        expand: str = DEFAULT_ISSUE_EXPAND,
        jira_fields: Optional[str] = None,
        fetch_all_comments: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if expand:
            params["expand"] = expand
        if jira_fields:
            params["fields"] = jira_fields
        data = self.parse_response(self.request("GET", f"/issue/{issue_key}", params=params or None))
        if not isinstance(data, dict):
            make_error(
                code="ISSUE_NOT_FOUND",
                category=ErrorCategory.CLIENT,
                message=f"Issue {issue_key} not found.",
                command=get_ctx().command,
            )
            raise AssertionError("unreachable")
        if fetch_all_comments:
            self._merge_all_comments(data, issue_key)
        return data

    def _merge_all_comments(self, issue: dict[str, Any], issue_key: str) -> None:
        fields = issue.get("fields")
        if not isinstance(fields, dict):
            return
        comment_page = fields.get("comment")
        existing: list[Any] = []
        total = 0
        if isinstance(comment_page, dict):
            comments = comment_page.get("comments")
            if isinstance(comments, list):
                existing = comments
            total = int(comment_page.get("total") or len(existing))
        elif isinstance(comment_page, list):
            existing = comment_page
            total = len(existing)
        if total <= len(existing):
            return
        all_comments = self.list_all_comments(issue_key, expand="renderedBody")
        fields["comment"] = {
            "comments": all_comments,
            "maxResults": len(all_comments),
            "total": len(all_comments),
            "startAt": 0,
        }

    def list_all_comments(self, issue_key: str, *, expand: str = "renderedBody") -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        start_at = 0
        max_results = 100
        while True:
            params: dict[str, Any] = {"startAt": start_at, "maxResults": max_results}
            if expand:
                params["expand"] = expand
            data = self.parse_response(self.request("GET", f"/issue/{issue_key}/comment", params=params))
            if not isinstance(data, dict):
                break
            page = data.get("comments", [])
            if isinstance(page, list):
                comments.extend(item for item in page if isinstance(item, dict))
            total = int(data.get("total") or len(comments))
            if start_at + (len(page) if isinstance(page, list) else 0) >= total or not page:
                break
            start_at += max_results
        return comments

    def list_attachments(self, issue_key: str) -> list[dict[str, Any]]:
        issue = self.get_issue(issue_key, expand="", jira_fields="attachment", fetch_all_comments=False)
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        attachments = fields.get("attachment") if isinstance(fields, dict) else None
        if not isinstance(attachments, list):
            return []
        return [item for item in attachments if isinstance(item, dict)]

    def download_url(self, url: str) -> httpx.Response:
        if url.startswith("http://") or url.startswith("https://"):
            try:
                return httpx.get(
                    url,
                    headers={"Authorization": f"Bearer {self._pat}"},
                    timeout=self._timeout,
                    follow_redirects=True,
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
            raise AssertionError("unreachable")
        return self.request_site("GET", url)

    def download_attachment_meta(self, attachment: dict[str, Any], output: Path) -> dict[str, Any]:
        content_url = attachment.get("content")
        if not content_url:
            make_error(
                code="ATTACHMENT_DOWNLOAD_LINK_MISSING",
                category=ErrorCategory.CLIENT,
                message=f"Attachment {attachment.get('id')} has no content URL.",
                command=get_ctx().command,
            )
        response = self.download_url(str(content_url))
        self.save_binary(response, output)
        return {
            "id": attachment.get("id"),
            "filename": attachment.get("filename"),
            "saved": str(output),
            "bytes": len(response.content),
            "mimeType": attachment.get("mimeType"),
        }

    def download_all_attachments(self, issue_key: str, output_dir: Path) -> list[dict[str, Any]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        attachments = self.list_attachments(issue_key)
        saved: list[dict[str, Any]] = []
        used_names: dict[str, int] = {}
        for attachment in attachments:
            filename = sanitize_filename(str(attachment.get("filename") or attachment.get("id") or "attachment"))
            if filename in used_names:
                used_names[filename] += 1
                stem = Path(filename)
                filename = f"{stem.stem}-{used_names[filename]}{stem.suffix}"
            else:
                used_names[filename] = 0
            saved.append(self.download_attachment_meta(attachment, output_dir / filename))
        return saved

    def search_jql_page(
        self,
        *,
        jql: str,
        start_at: int,
        max_results: int,
        issue_fields: Optional[str],
        expand: Optional[str],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"jql": jql, "startAt": start_at, "maxResults": max_results}
        parsed_fields = parse_csv_list(issue_fields)
        if parsed_fields:
            body["fields"] = parsed_fields
        parsed_expand = parse_csv_list(expand)
        if parsed_expand:
            body["expand"] = parsed_expand
        data = self.parse_response(self.request("POST", "/search", json_body=body))
        if not isinstance(data, dict):
            return {"issues": [], "total": 0, "startAt": start_at, "maxResults": max_results, "_raw": data}
        return data


def get_client() -> JiraClient:
    pat, api_url = load_config()
    return JiraClient(pat, api_url, get_ctx().timeout, get_ctx().api_version)


def run_api(
    handler: Callable[[JiraClient], Any],
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

_swagger_cache: dict[str, dict[str, Any]] = {}


def swagger_path_for(spec: str) -> Path:
    if spec == "platform":
        return PLATFORM_SWAGGER_PATH
    if spec == "software":
        return SOFTWARE_SWAGGER_PATH
    make_error(
        code="INVALID_SPEC",
        category=ErrorCategory.VALIDATION,
        message=f"Unknown swagger spec: {spec}",
        hint="Use --spec platform, software, or all.",
        command=get_ctx().command or "openapi",
        exit_code=EXIT_USAGE,
    )
    raise AssertionError("unreachable")


def load_swagger(spec: str) -> dict[str, Any]:
    cached = _swagger_cache.get(spec)
    if cached is not None:
        return cached
    path = swagger_path_for(spec)
    if not path.exists():
        make_error(
            code="SWAGGER_MISSING",
            category=ErrorCategory.CONFIG,
            message=f"Swagger file not found: {path}",
            command=get_ctx().command or "openapi",
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        make_error(
            code="SWAGGER_INVALID",
            category=ErrorCategory.CONFIG,
            message=f"Swagger file is not a JSON object: {path}",
            command=get_ctx().command or "openapi",
        )
        raise AssertionError("unreachable")
    _swagger_cache[spec] = loaded
    return loaded


def list_openapi_operations(
    *,
    spec: str = "all",
    tag: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    specs = ["platform", "software"] if spec == "all" else [spec]
    ops: list[dict[str, Any]] = []
    for spec_name in specs:
        swagger = load_swagger(spec_name)
        for path, methods in swagger.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, spec_body in methods.items():
                if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                    continue
                if not isinstance(spec_body, dict):
                    continue
                tags = spec_body.get("tags", [])
                if tag and tag not in tags:
                    continue
                ops.append(
                    {
                        "spec": spec_name,
                        "method": method.upper(),
                        "path": path,
                        "operationId": spec_body.get("operationId"),
                        "summary": spec_body.get("summary"),
                        "tags": tags,
                    }
                )
    ops.sort(key=lambda item: (str(item["spec"]), str(item["path"]), str(item["method"])))
    if limit is not None:
        ops = ops[:limit]
    return ops


def describe_openapi_operation(operation_id: str, *, spec: str = "all") -> dict[str, Any]:
    specs = ["platform", "software"] if spec == "all" else [spec]
    for spec_name in specs:
        swagger = load_swagger(spec_name)
        for path, methods in swagger.get("paths", {}).items():
            if not isinstance(methods, dict):
                continue
            for method, spec_body in methods.items():
                if not isinstance(spec_body, dict):
                    continue
                if spec_body.get("operationId") == operation_id:
                    return {
                        "spec": spec_name,
                        "method": method.upper(),
                        "path": path,
                        "operationId": operation_id,
                        "summary": spec_body.get("summary"),
                        "tags": spec_body.get("tags", []),
                        "parameters": spec_body.get("parameters", []),
                        "requestBody": spec_body.get("requestBody"),
                        "responses": spec_body.get("responses", {}),
                    }
    make_error(
        code="OPERATION_NOT_FOUND",
        category=ErrorCategory.VALIDATION,
        message=f"operationId not found: {operation_id}",
        hint="Run openapi list, or pass --spec platform|software.",
        command=get_ctx().command or "openapi describe",
        exit_code=EXIT_USAGE,
    )
    raise AssertionError("unreachable")


TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "name": "jira_schema",
        "description": "Get CLI tool definitions for AI agents",
        "input_schema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["default", "openai", "anthropic"], "default": "default"},
            },
        },
    },
    {
        "name": "jira_openapi_list",
        "description": "List Jira REST API operations from bundled Cloud swagger (map /rest/api/3 to /rest/api/2 on DC)",
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {"type": "string", "enum": ["platform", "software", "all"], "default": "all"},
                "tag": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "jira_openapi_describe",
        "description": "Describe a Jira REST API operation by operationId",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation_id": {"type": "string"},
                "spec": {"type": "string", "enum": ["platform", "software", "all"], "default": "all"},
            },
            "required": ["operation_id"],
        },
    },
    {
        "name": "jira_request",
        "description": "Generic HTTP request to any Jira REST endpoint",
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
        "name": "jira_server_info",
        "description": "Get Jira server information",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jira_search_jql",
        "description": "Search Jira issues using JQL",
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
                "start": {"type": "integer", "default": 0},
                "expand": {"type": "string", "default": "renderedFields"},
                "issue_fields": {"type": "string"},
                "all": {"type": "boolean"},
                "stream": {"type": "boolean"},
                "fields": {"type": "string"},
            },
            "required": ["jql"],
        },
    },
    {
        "name": "jira_issue_get",
        "description": "Get a Jira issue by key, numeric id, or browse URL",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "expand": {"type": "string", "default": DEFAULT_ISSUE_EXPAND},
                "jira_fields": {"type": "string"},
                "fields": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_issue_resolve_url",
        "description": "Extract Jira issue key from a browse URL or key string",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "jira_issue_export",
        "description": "Export issue JSON, description HTML/wiki, comments, and attachments to a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "output_dir": {
                    "type": "string",
                    "description": "Export directory (default: scripts/output/scex-jira/<KEY>).",
                },
                "expand": {"type": "string", "default": DEFAULT_ISSUE_EXPAND},
                "skip_attachments": {"type": "boolean", "default": False},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_issue_create",
        "description": "Create a Jira issue (wiki markup description on API v2)",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "summary": {"type": "string"},
                "issuetype": {"type": "string"},
                "description": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
        },
    },
    {
        "name": "jira_issue_update",
        "description": "Update a Jira issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "summary": {"type": "string"},
                "description": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
                "notify_users": {"type": "boolean"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_issue_delete",
        "description": "Delete a Jira issue (requires force)",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "delete_subtasks": {"type": "boolean"},
                "force": {"type": "boolean"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_issue_assign",
        "description": "Assign a Jira issue by DC username",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "name": {"type": "string", "description": "Jira username; omit or null to unassign"},
                "account_id": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_comment_list",
        "description": "List comments on a Jira issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "expand": {"type": "string", "default": "renderedBody"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
                "all": {"type": "boolean"},
                "fields": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_comment_get",
        "description": "Get one comment on a Jira issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "comment_id": {"type": "string"},
                "expand": {"type": "string", "default": "renderedBody"},
            },
            "required": ["issue", "comment_id"],
        },
    },
    {
        "name": "jira_comment_add",
        "description": "Add a comment (wiki markup on API v2)",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "text": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_comment_update",
        "description": "Update a comment",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "comment_id": {"type": "string"},
                "text": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
            "required": ["issue", "comment_id"],
        },
    },
    {
        "name": "jira_comment_delete",
        "description": "Delete a comment (requires force)",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "comment_id": {"type": "string"},
                "force": {"type": "boolean"},
            },
            "required": ["issue", "comment_id"],
        },
    },
    {
        "name": "jira_attachment_list",
        "description": "List attachments on a Jira issue",
        "input_schema": {
            "type": "object",
            "properties": {"issue": {"type": "string"}},
            "required": ["issue"],
        },
    },
    {
        "name": "jira_attachment_upload",
        "description": "Upload a file attachment to a Jira issue",
        "input_schema": {
            "type": "object",
            "properties": {"issue": {"type": "string"}, "file": {"type": "string"}},
            "required": ["issue", "file"],
        },
    },
    {
        "name": "jira_attachment_download",
        "description": "Download one attachment binary",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "attachment_id": {"type": "string"},
                "output": {"type": "string"},
            },
            "required": ["issue", "attachment_id", "output"],
        },
    },
    {
        "name": "jira_attachment_download_all",
        "description": "Download all attachments for an issue into a directory",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "output_dir": {
                    "type": "string",
                    "description": "Download directory (default: scripts/output/scex-jira/<KEY>).",
                },
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_transition_list",
        "description": "List available transitions for an issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "expand": {"type": "string"},
            },
            "required": ["issue"],
        },
    },
    {
        "name": "jira_transition_do",
        "description": "Perform a workflow transition on an issue",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue": {"type": "string"},
                "transition_id": {"type": "string"},
                "body_json": {"type": "string"},
                "body_stdin": {"type": "boolean"},
                "body_file": {"type": "string"},
            },
            "required": ["issue", "transition_id"],
        },
    },
    {
        "name": "jira_user_current",
        "description": "Get the current Jira user (myself)",
        "input_schema": {"type": "object", "properties": {"expand": {"type": "string"}}},
    },
    {
        "name": "jira_user_search",
        "description": "Search Jira users",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "jira_project_list",
        "description": "List Jira projects",
        "input_schema": {
            "type": "object",
            "properties": {"recent": {"type": "integer"}, "fields": {"type": "string"}},
        },
    },
    {
        "name": "jira_project_get",
        "description": "Get a Jira project by key or id",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "expand": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "jira_board_list",
        "description": "List Jira Software boards",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "project_key_or_id": {"type": "string"},
                "type": {"type": "string"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
                "all": {"type": "boolean"},
                "fields": {"type": "string"},
            },
        },
    },
    {
        "name": "jira_board_get",
        "description": "Get a Jira Software board by id",
        "input_schema": {
            "type": "object",
            "properties": {"board_id": {"type": "integer"}},
            "required": ["board_id"],
        },
    },
    {
        "name": "jira_sprint_list",
        "description": "List sprints for a board",
        "input_schema": {
            "type": "object",
            "properties": {
                "board_id": {"type": "integer"},
                "state": {"type": "string"},
                "limit": {"type": "integer"},
                "start": {"type": "integer"},
                "all": {"type": "boolean"},
                "fields": {"type": "string"},
            },
            "required": ["board_id"],
        },
    },
    {
        "name": "jira_sprint_get",
        "description": "Get a sprint by id",
        "input_schema": {
            "type": "object",
            "properties": {"sprint_id": {"type": "integer"}},
            "required": ["sprint_id"],
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
    if format_name == "default":
        return {
            "tools": TOOL_REGISTRY,
            "response_envelope": envelope,
            "stream_format": {
                "description": "NDJSON events: progress | item | done | error",
                "note": "When --stream is set, CliResult envelope is bypassed; last line is event=done.",
            },
        }
    make_error(
        code="INVALID_SCHEMA_FORMAT",
        category=ErrorCategory.VALIDATION,
        message=f"Unknown schema format: {format_name}",
        hint="Use --format default|openai|anthropic.",
        command="schema",
        exit_code=EXIT_USAGE,
    )
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def stream_jql(
    client: JiraClient,
    *,
    jql: str,
    command: str,
    limit: int,
    start: int,
    fetch_all: bool,
    issue_fields: Optional[str],
    expand: Optional[str],
    fields: Optional[str],
) -> None:
    start_time = time.monotonic()
    count = 0
    page = 0
    current_start = start
    total: Optional[int] = None

    while True:
        page += 1
        data = client.search_jql_page(
            jql=jql,
            start_at=current_start,
            max_results=limit,
            issue_fields=issue_fields,
            expand=expand,
        )
        issues = data.get("issues", [])
        total = int(data.get("total") or 0)
        if isinstance(issues, list):
            for item in issues:
                emit_stream_event({"event": "item", "data": project_fields(item, fields)})
                count += 1
        emit_stream_event({"event": "progress", "data": {"page": page, "fetched": count, "total": total}})
        if not fetch_all:
            break
        page_len = len(issues) if isinstance(issues, list) else 0
        current_start += page_len
        if page_len == 0 or current_start >= total:
            break

    duration_ms = int((time.monotonic() - start_time) * 1000)
    extra = {"total": total} if total is not None else None
    emit_stream_done(command=command, count=count, duration_ms=duration_ms, extra_meta=extra)


def stream_agile_values(
    client: JiraClient,
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

    while True:
        page += 1
        page_params = {**params, "maxResults": str(limit), "startAt": str(current_start)}
        data = client.parse_response(client.request_agile("GET", path, params=page_params))
        if not isinstance(data, dict):
            emit_stream_event({"event": "item", "data": data})
            count += 1
            break
        values = data.get("values", [])
        if isinstance(values, list):
            for item in values:
                emit_stream_event({"event": "item", "data": project_fields(item, fields)})
                count += 1
        emit_stream_event({"event": "progress", "data": {"page": page, "fetched": count}})
        if not fetch_all:
            break
        page_len = len(values) if isinstance(values, list) else 0
        if data.get("isLast") is True or page_len < limit or page_len == 0:
            break
        current_start += page_len

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


def unique_filename(filename: str, used_names: dict[str, int]) -> str:
    if filename not in used_names:
        used_names[filename] = 0
        return filename
    used_names[filename] += 1
    stem = Path(filename)
    return f"{stem.stem}-{used_names[filename]}{stem.suffix}"


def export_issue_to_dir(
    client: JiraClient,
    issue_key: str,
    output_dir: Path | None,
    *,
    expand: str,
    skip_attachments: bool,
) -> dict[str, Any]:
    issue = client.get_issue(issue_key, expand=expand, fetch_all_comments=True)
    key = str(issue.get("key") or issue_key)
    resolved_output_dir = resolve_export_output_dir(key, output_dir=output_dir)
    resolved_output_dir.mkdir(parents=True, exist_ok=True)

    issue_path = resolved_output_dir / "issue.json"
    issue_path.write_text(json.dumps(issue, ensure_ascii=False, indent=2), encoding="utf-8")

    html, wiki = description_parts(issue)
    (resolved_output_dir / "description.html").write_text(html, encoding="utf-8")
    (resolved_output_dir / "description.wiki").write_text(wiki, encoding="utf-8")

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    comment_block = fields.get("comment") if isinstance(fields, dict) else None
    comments: list[Any] = []
    if isinstance(comment_block, dict) and isinstance(comment_block.get("comments"), list):
        comments = comment_block["comments"]
    elif isinstance(comment_block, list):
        comments = comment_block
    comments_path = resolved_output_dir / "comments.json"
    comments_path.write_text(json.dumps({"comments": comments, "total": len(comments)}, ensure_ascii=False, indent=2), encoding="utf-8")

    attachments = fields.get("attachment") if isinstance(fields, dict) else None
    if not isinstance(attachments, list):
        attachments = client.list_attachments(key)
    attachments_path = resolved_output_dir / "attachments.json"
    attachments_path.write_text(json.dumps({"attachments": attachments}, ensure_ascii=False, indent=2), encoding="utf-8")

    downloaded: list[dict[str, Any]] = []
    if not skip_attachments and isinstance(attachments, list) and attachments:
        used_names: dict[str, int] = {}
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            filename = unique_filename(
                sanitize_filename(str(attachment.get("filename") or attachment.get("id") or "attachment")),
                used_names,
            )
            downloaded.append(client.download_attachment_meta(attachment, resolved_output_dir / filename))

    summary = fields.get("summary") if isinstance(fields, dict) else None
    status = fields.get("status") if isinstance(fields, dict) else None
    status_name = status.get("name") if isinstance(status, dict) else None
    manifest = {
        "key": key,
        "id": issue.get("id"),
        "summary": summary,
        "status": status_name,
        "output_dir": str(resolved_output_dir),
        "files": {
            "issue": issue_path.name,
            "description_html": "description.html",
            "description_wiki": "description.wiki",
            "comments": comments_path.name,
            "attachments_index": attachments_path.name,
            "manifest": "manifest.json",
        },
        "comment_count": len(comments),
        "attachments": downloaded,
    }
    (resolved_output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def merge_issue_fields(
    payload: Optional[Any],
    *,
    project: Optional[str],
    summary: Optional[str],
    issuetype: Optional[str],
    description: Optional[str],
    command: str,
) -> dict[str, Any]:
    if payload is None:
        body: dict[str, Any] = {"fields": {}}
    elif isinstance(payload, dict):
        body = dict(payload)
        body.setdefault("fields", {})
    else:
        make_error(
            code="BODY_JSON_INVALID",
            category=ErrorCategory.VALIDATION,
            message="Issue body must be a JSON object.",
            command=command,
            exit_code=EXIT_USAGE,
        )
        raise AssertionError("unreachable")

    fields = body["fields"]
    if not isinstance(fields, dict):
        make_error(
            code="BODY_JSON_INVALID",
            category=ErrorCategory.VALIDATION,
            message="Issue body.fields must be an object.",
            command=command,
            exit_code=EXIT_USAGE,
        )
        raise AssertionError("unreachable")
    if project is not None:
        fields["project"] = {"key": project}
    if summary is not None:
        fields["summary"] = summary
    if issuetype is not None:
        fields["issuetype"] = {"name": issuetype}
    if description is not None:
        fields["description"] = description
    return body


def comment_payload(
    *,
    text: Optional[str],
    body_json: Optional[str],
    body_stdin: bool,
    body_file: Optional[str],
    command: str,
) -> dict[str, Any]:
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    if payload is not None:
        if not isinstance(payload, dict):
            make_error(
                code="BODY_JSON_INVALID",
                category=ErrorCategory.VALIDATION,
                message="Comment body must be a JSON object.",
                command=command,
                exit_code=EXIT_USAGE,
            )
            raise AssertionError("unreachable")
        if text is not None:
            payload["body"] = text
        return payload
    if text is None:
        make_error(
            code="MISSING_COMMENT_BODY",
            category=ErrorCategory.VALIDATION,
            message="Provide --text or a JSON body.",
            hint="Example: comment add ITSD-133 --text 'looks good'",
            command=command,
            exit_code=EXIT_USAGE,
        )
        raise AssertionError("unreachable")
    return {"body": text}


# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------


class OrderedTyperGroup(TyperGroup):
    def list_commands(self, ctx: typer.Context) -> list[str]:
        return sorted(self.commands.keys())


app = typer.Typer(
    name="scex-jira",
    help="AI-friendly CLI for Jira Data Center REST API.",
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
    api_url: Optional[str] = typer.Option(None, "--api-url", help="Jira base URL."),
    timeout: float = typer.Option(DEFAULT_TIMEOUT, "--timeout", help="HTTP timeout in seconds."),
    force: bool = typer.Option(False, "--force", help="Skip destructive operation warnings."),
    api_version: str = typer.Option("2", "--api-version", help="REST API version: 2 (DC default) or 3 (Cloud)."),
) -> None:
    if api_version not in ("2", "3"):
        make_error(
            code="INVALID_API_VERSION",
            category=ErrorCategory.VALIDATION,
            message=f"Unsupported API version: {api_version}",
            hint="Use --api-version 2 or 3.",
            command="config",
            exit_code=EXIT_USAGE,
        )
    _ctx.json_output = json_output
    _ctx.quiet = quiet
    _ctx.stream = stream
    _ctx.pat = pat
    _ctx.api_url = api_url
    _ctx.timeout = timeout
    _ctx.force = force
    _ctx.api_version = api_version


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

openapi_app = typer.Typer(help="Browse bundled OpenAPI swagger (Jira Cloud specs).")
app.add_typer(openapi_app, name="openapi")


@openapi_app.command("list")
def openapi_list(
    spec: str = typer.Option("all", "--spec", help="platform|software|all"),
    tag: Optional[str] = typer.Option(None, "--tag"),
    limit: Optional[int] = typer.Option(None, "--limit"),
) -> None:
    get_ctx().command = "openapi list"
    if spec not in ("platform", "software", "all"):
        make_error(
            code="INVALID_SPEC",
            category=ErrorCategory.VALIDATION,
            message=f"Unknown swagger spec: {spec}",
            hint="Use --spec platform, software, or all.",
            command="openapi list",
            exit_code=EXIT_USAGE,
        )
    ops = list_openapi_operations(spec=spec, tag=tag, limit=limit)
    emit(make_ok({"count": len(ops), "operations": ops}, command="openapi list"))


@openapi_app.command("describe")
def openapi_describe(
    operation_id: str = typer.Argument(..., help="operationId from swagger"),
    spec: str = typer.Option("all", "--spec", help="platform|software|all"),
) -> None:
    get_ctx().command = "openapi describe"
    if spec not in ("platform", "software", "all"):
        make_error(
            code="INVALID_SPEC",
            category=ErrorCategory.VALIDATION,
            message=f"Unknown swagger spec: {spec}",
            hint="Use --spec platform, software, or all.",
            command="openapi describe",
            exit_code=EXIT_USAGE,
        )
    emit(make_ok(describe_openapi_operation(operation_id, spec=spec), command="openapi describe"))


# --- request ---


@app.command("request")
def request_cmd(
    method: str = typer.Argument(..., help="HTTP method"),
    path: str = typer.Argument(..., help="API path e.g. /rest/api/2/issue/ITSD-133"),
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

    def handler(client: JiraClient) -> Any:
        if is_site_path(path):
            response = client.request_site(
                method,
                path,
                params=params or None,
                headers=headers or None,
            )
        elif path.startswith("/rest/agile"):
            response = client.request_agile(
                method,
                path,
                params=params or None,
                json_body=json_body,
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
    def handler(client: JiraClient) -> Any:
        response = client.request("GET", "/serverInfo")
        if response.status_code == 404 and client.api_version == "2":
            response = client.request("GET", "/rest/api/3/serverInfo")
        return client.parse_response(response)

    run_api(handler, command="server info")


# --- search ---

search_app = typer.Typer(help="Search Jira via JQL.")
app.add_typer(search_app, name="search")


@search_app.command("jql")
def search_jql(
    jql: str = typer.Argument(..., help="JQL query string"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    expand: Optional[str] = typer.Option(DEFAULT_SEARCH_EXPAND, "--expand"),
    issue_fields: Optional[str] = typer.Option(None, "--issue-fields", help="Jira fields param, e.g. summary,status"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "search jql"
    get_ctx().command = command

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            if get_ctx().stream:
                stream_jql(
                    client,
                    jql=jql,
                    command=command,
                    limit=limit,
                    start=start,
                    fetch_all=fetch_all,
                    issue_fields=issue_fields,
                    expand=expand,
                    fields=fields,
                )
            else:
                issues: list[Any] = []
                current_start = start
                total = 0
                while True:
                    data = client.search_jql_page(
                        jql=jql,
                        start_at=current_start,
                        max_results=limit,
                        issue_fields=issue_fields,
                        expand=expand,
                    )
                    page_issues = data.get("issues", [])
                    total = int(data.get("total") or 0)
                    if isinstance(page_issues, list):
                        issues.extend(page_issues)
                        current_start += len(page_issues)
                        if not page_issues:
                            break
                    else:
                        break
                    if current_start >= total:
                        break
                emit(
                    make_ok(
                        project_fields({"issues": issues, "total": total, "startAt": start, "maxResults": limit}, fields),
                        command=command,
                    )
                )
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: JiraClient) -> Any:
        data = client.search_jql_page(
            jql=jql,
            start_at=start,
            max_results=limit,
            issue_fields=issue_fields,
            expand=expand,
        )
        return project_fields(data, fields)

    run_api(handler, command=command)


# --- issue ---

issue_app = typer.Typer(help="Issue operations.")
app.add_typer(issue_app, name="issue")


@issue_app.command("get")
def issue_get(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    expand: str = typer.Option(DEFAULT_ISSUE_EXPAND, "--expand"),
    jira_fields: Optional[str] = typer.Option(None, "--jira-fields", help="Jira fields query param"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    key = parse_issue_key(issue, command="issue get")

    def handler(client: JiraClient) -> Any:
        data = client.get_issue(key, expand=expand, jira_fields=jira_fields, fetch_all_comments=True)
        return project_fields(data, fields)

    run_api(handler, command="issue get")


@issue_app.command("resolve-url")
def issue_resolve_url(url: str = typer.Argument(..., help="Jira browse URL or issue key")) -> None:
    command = "issue resolve-url"
    get_ctx().command = command
    key = parse_issue_key(url, command=command)
    emit(make_ok({"key": key, "input": url}, command=command))


@issue_app.command("export")
def issue_export(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Export directory (default: scripts/output/scex-jira/<KEY>).",
    ),
    expand: str = typer.Option(DEFAULT_ISSUE_EXPAND, "--expand"),
    skip_attachments: bool = typer.Option(False, "--skip-attachments"),
) -> None:
    key = parse_issue_key(issue, command="issue export")

    def handler(client: JiraClient) -> Any:
        return export_issue_to_dir(
            client,
            key,
            output_dir,
            expand=expand,
            skip_attachments=skip_attachments,
        )

    run_api(handler, command="issue export")


@issue_app.command("create")
def issue_create(
    project: Optional[str] = typer.Option(None, "--project", help="Project key"),
    summary: Optional[str] = typer.Option(None, "--summary"),
    issuetype: Optional[str] = typer.Option(None, "--issuetype", help="Issue type name, e.g. Task"),
    description: Optional[str] = typer.Option(None, "--description", help="Wiki markup (API v2)"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json or path"),
) -> None:
    command = "issue create"
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    body = merge_issue_fields(
        payload,
        project=project,
        summary=summary,
        issuetype=issuetype,
        description=description,
        command=command,
    )
    fields = body.get("fields", {})
    if not isinstance(fields, dict) or not fields:
        make_error(
            code="MISSING_ISSUE_FIELDS",
            category=ErrorCategory.VALIDATION,
            message="Provide --project/--summary/--issuetype or a JSON body with fields.",
            command=command,
            exit_code=EXIT_USAGE,
        )

    def handler(client: JiraClient) -> Any:
        return client.parse_response(client.request("POST", "/issue", json_body=body))

    run_api(handler, command=command)


@issue_app.command("update")
def issue_update(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    summary: Optional[str] = typer.Option(None, "--summary"),
    description: Optional[str] = typer.Option(None, "--description", help="Wiki markup (API v2)"),
    notify_users: Optional[bool] = typer.Option(None, "--notify-users/--no-notify-users"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json or path"),
) -> None:
    command = "issue update"
    key = parse_issue_key(issue, command=command)
    payload = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    body = merge_issue_fields(
        payload,
        project=None,
        summary=summary,
        issuetype=None,
        description=description,
        command=command,
    )

    def handler(client: JiraClient) -> Any:
        params: dict[str, Any] = {}
        if notify_users is not None:
            params["notifyUsers"] = str(notify_users).lower()
        result = client.parse_response(client.request("PUT", f"/issue/{key}", params=params or None, json_body=body))
        return result if result is not None else {"updated": key}

    run_api(handler, command=command)


@issue_app.command("delete")
def issue_delete(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    delete_subtasks: bool = typer.Option(False, "--delete-subtasks"),
) -> None:
    command = "issue delete"
    require_force(command)
    key = parse_issue_key(issue, command=command)

    def handler(client: JiraClient) -> Any:
        params = {"deleteSubtasks": "true"} if delete_subtasks else None
        result = client.parse_response(client.request("DELETE", f"/issue/{key}", params=params))
        return result if result is not None else {"deleted": key}

    run_api(handler, command=command)


@issue_app.command("assign")
def issue_assign(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    name: Optional[str] = typer.Option(None, "--name", help="Jira username (Data Center)"),
    account_id: Optional[str] = typer.Option(None, "--account-id", help="Cloud accountId"),
    unassign: bool = typer.Option(False, "--unassign"),
) -> None:
    command = "issue assign"
    key = parse_issue_key(issue, command=command)
    if unassign:
        body: Any = {"name": None}
    elif account_id:
        body = {"accountId": account_id}
    elif name:
        body = {"name": name}
    else:
        make_error(
            code="MISSING_ASSIGNEE",
            category=ErrorCategory.VALIDATION,
            message="Provide --name, --account-id, or --unassign.",
            command=command,
            exit_code=EXIT_USAGE,
        )
        raise AssertionError("unreachable")

    def handler(client: JiraClient) -> Any:
        result = client.parse_response(client.request("PUT", f"/issue/{key}/assignee", json_body=body))
        return result if result is not None else {"assigned": key, "assignee": body}

    run_api(handler, command=command)


# --- comment ---

comment_app = typer.Typer(help="Issue comments.")
app.add_typer(comment_app, name="comment")


@comment_app.command("list")
def comment_list(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    expand: str = typer.Option("renderedBody", "--expand"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "comment list"
    key = parse_issue_key(issue, command=command)

    def handler(client: JiraClient) -> Any:
        if fetch_all:
            comments = client.list_all_comments(key, expand=expand)
            data = {"comments": comments, "total": len(comments), "startAt": 0, "maxResults": len(comments)}
            return project_fields(data, fields)
        params: dict[str, Any] = {"startAt": start, "maxResults": limit}
        if expand:
            params["expand"] = expand
        data = client.parse_response(client.request("GET", f"/issue/{key}/comment", params=params))
        return project_fields(data, fields)

    run_api(handler, command=command)


@comment_app.command("get")
def comment_get(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    comment_id: str = typer.Argument(..., help="Comment id"),
    expand: str = typer.Option("renderedBody", "--expand"),
) -> None:
    key = parse_issue_key(issue, command="comment get")

    def handler(client: JiraClient) -> Any:
        params = {"expand": expand} if expand else None
        return client.parse_response(client.request("GET", f"/issue/{key}/comment/{comment_id}", params=params))

    run_api(handler, command="comment get")


@comment_app.command("add")
def comment_add(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    text: Optional[str] = typer.Option(None, "--text", help="Wiki markup comment body (API v2)"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json or path"),
) -> None:
    command = "comment add"
    key = parse_issue_key(issue, command=command)
    payload = comment_payload(
        text=text,
        body_json=body_json,
        body_stdin=body_stdin,
        body_file=body_file,
        command=command,
    )

    def handler(client: JiraClient) -> Any:
        return client.parse_response(
            client.request("POST", f"/issue/{key}/comment", json_body=payload, params={"expand": "renderedBody"})
        )

    run_api(handler, command=command)


@comment_app.command("update")
def comment_update(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    comment_id: str = typer.Argument(..., help="Comment id"),
    text: Optional[str] = typer.Option(None, "--text", help="Wiki markup comment body (API v2)"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json or path"),
) -> None:
    command = "comment update"
    key = parse_issue_key(issue, command=command)
    payload = comment_payload(
        text=text,
        body_json=body_json,
        body_stdin=body_stdin,
        body_file=body_file,
        command=command,
    )

    def handler(client: JiraClient) -> Any:
        return client.parse_response(client.request("PUT", f"/issue/{key}/comment/{comment_id}", json_body=payload))

    run_api(handler, command=command)


@comment_app.command("delete")
def comment_delete(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    comment_id: str = typer.Argument(..., help="Comment id"),
) -> None:
    command = "comment delete"
    require_force(command)
    key = parse_issue_key(issue, command=command)

    def handler(client: JiraClient) -> Any:
        result = client.parse_response(client.request("DELETE", f"/issue/{key}/comment/{comment_id}"))
        return result if result is not None else {"deleted": comment_id, "issue": key}

    run_api(handler, command=command)


# --- attachment ---

attachment_app = typer.Typer(help="Issue attachments.")
app.add_typer(attachment_app, name="attachment")


@attachment_app.command("list")
def attachment_list(issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL")) -> None:
    key = parse_issue_key(issue, command="attachment list")

    def handler(client: JiraClient) -> Any:
        attachments = client.list_attachments(key)
        return {"issue": key, "attachments": attachments, "total": len(attachments)}

    run_api(handler, command="attachment list")


@attachment_app.command("upload")
def attachment_upload(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    file: Path = typer.Option(..., "--file", exists=True, readable=True, dir_okay=False),
) -> None:
    key = parse_issue_key(issue, command="attachment upload")

    def handler(client: JiraClient) -> Any:
        with file.open("rb") as handle:
            files = {"file": (file.name, handle)}
            response = client.request(
                "POST",
                f"/issue/{key}/attachments",
                files=files,
                headers={"X-Atlassian-Token": "no-check"},
            )
        return client.parse_response(response)

    run_api(handler, command="attachment upload")


@attachment_app.command("download")
def attachment_download(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    attachment_id: str = typer.Argument(..., help="Attachment id"),
    output: Path = typer.Option(..., "--output", help="Output file path"),
) -> None:
    key = parse_issue_key(issue, command="attachment download")

    def handler(client: JiraClient) -> Any:
        attachments = client.list_attachments(key)
        match = next((item for item in attachments if str(item.get("id")) == str(attachment_id)), None)
        if match is None:
            meta = client.parse_response(client.request("GET", f"/attachment/{attachment_id}"))
            if not isinstance(meta, dict):
                make_error(
                    code="ATTACHMENT_NOT_FOUND",
                    category=ErrorCategory.CLIENT,
                    message=f"Attachment {attachment_id} not found on {key}.",
                    command="attachment download",
                )
                raise AssertionError("unreachable")
            match = meta
        return client.download_attachment_meta(match, output)

    run_api(handler, command="attachment download")


@attachment_app.command("download-all")
def attachment_download_all(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    output_dir: Optional[Path] = typer.Option(
        None,
        "--output-dir",
        help="Download directory (default: scripts/output/scex-jira/<KEY>).",
    ),
) -> None:
    key = parse_issue_key(issue, command="attachment download-all")

    def handler(client: JiraClient) -> Any:
        resolved = resolve_export_output_dir(key, output_dir=output_dir)
        downloaded = client.download_all_attachments(key, resolved)
        return {"issue": key, "output_dir": str(resolved), "attachments": downloaded}

    run_api(handler, command="attachment download-all")


# --- transition ---

transition_app = typer.Typer(help="Issue workflow transitions.")
app.add_typer(transition_app, name="transition")


@transition_app.command("list")
def transition_list(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    expand: Optional[str] = typer.Option(None, "--expand", help="e.g. transitions.fields"),
) -> None:
    key = parse_issue_key(issue, command="transition list")

    def handler(client: JiraClient) -> Any:
        params = {"expand": expand} if expand else None
        return client.parse_response(client.request("GET", f"/issue/{key}/transitions", params=params))

    run_api(handler, command="transition list")


@transition_app.command("do")
def transition_do(
    issue: str = typer.Argument(..., help="Issue key, numeric id, or browse URL"),
    transition_id: str = typer.Option(..., "--transition-id"),
    body_json: Optional[str] = typer.Option(None, "--body-json"),
    body_stdin: bool = typer.Option(False, "--body-stdin"),
    body_file: Optional[str] = typer.Option(None, "--body", help="@file.json extra payload"),
) -> None:
    command = "transition do"
    key = parse_issue_key(issue, command=command)
    extra = resolve_body(body_json=body_json, body_stdin=body_stdin, body_file=body_file, command=command)
    payload: dict[str, Any] = {"transition": {"id": transition_id}}
    if isinstance(extra, dict):
        payload.update(extra)
        payload["transition"] = {"id": transition_id}

    def handler(client: JiraClient) -> Any:
        result = client.parse_response(client.request("POST", f"/issue/{key}/transitions", json_body=payload))
        return result if result is not None else {"transitioned": key, "transition": transition_id}

    run_api(handler, command=command)


# --- user ---

user_app = typer.Typer(help="User operations.")
app.add_typer(user_app, name="user")


@user_app.command("current")
def user_current(expand: Optional[str] = typer.Option(None, "--expand")) -> None:
    def handler(client: JiraClient) -> Any:
        params = {"expand": expand} if expand else None
        return client.parse_response(client.request("GET", "/myself", params=params))

    run_api(handler, command="user current")


@user_app.command("search")
def user_search(
    query: str = typer.Option(..., "--query", help="Username or display-name query"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
) -> None:
    def handler(client: JiraClient) -> Any:
        params: dict[str, Any] = {"maxResults": limit, "startAt": start}
        if client.api_version == "3":
            params["query"] = query
        else:
            params["username"] = query
        return client.parse_response(client.request("GET", "/user/search", params=params))

    run_api(handler, command="user search")


# --- project ---

project_app = typer.Typer(help="Project operations.")
app.add_typer(project_app, name="project")


@project_app.command("list")
def project_list(
    recent: Optional[int] = typer.Option(None, "--recent"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    def handler(client: JiraClient) -> Any:
        params: dict[str, Any] = {}
        if recent is not None:
            params["recent"] = recent
        data = client.parse_response(client.request("GET", "/project", params=params or None))
        if isinstance(data, list):
            data = {"projects": data, "total": len(data)}
        return project_fields(data, fields)

    run_api(handler, command="project list")


@project_app.command("get")
def project_get(
    key: str = typer.Argument(..., help="Project key or id"),
    expand: Optional[str] = typer.Option(None, "--expand"),
) -> None:
    def handler(client: JiraClient) -> Any:
        params = {"expand": expand} if expand else None
        return client.parse_response(client.request("GET", f"/project/{key}", params=params))

    run_api(handler, command="project get")


# --- board ---

board_app = typer.Typer(help="Jira Software boards.")
app.add_typer(board_app, name="board")


@board_app.command("list")
def board_list(
    name: Optional[str] = typer.Option(None, "--name"),
    project_key_or_id: Optional[str] = typer.Option(None, "--project-key-or-id"),
    board_type: Optional[str] = typer.Option(None, "--type"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "board list"
    get_ctx().command = command
    params: dict[str, Any] = {}
    if name:
        params["name"] = name
    if project_key_or_id:
        params["projectKeyOrId"] = project_key_or_id
    if board_type:
        params["type"] = board_type

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            if get_ctx().stream:
                stream_agile_values(
                    client,
                    "/board",
                    params,
                    command=command,
                    limit=limit,
                    start=start,
                    fetch_all=fetch_all,
                    fields=fields,
                )
            else:
                values: list[Any] = []
                current_start = start
                while True:
                    page_params = {**params, "maxResults": str(limit), "startAt": str(current_start)}
                    data = client.parse_response(client.request_agile("GET", "/board", params=page_params))
                    if not isinstance(data, dict):
                        break
                    page_values = data.get("values", [])
                    if isinstance(page_values, list):
                        values.extend(page_values)
                        current_start += len(page_values)
                    if data.get("isLast") is True or not page_values or len(page_values) < limit:
                        break
                emit(make_ok(project_fields({"values": values, "total": len(values)}, fields), command=command))
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: JiraClient) -> Any:
        page_params = {**params, "maxResults": str(limit), "startAt": str(start)}
        data = client.parse_response(client.request_agile("GET", "/board", params=page_params))
        return project_fields(data, fields)

    run_api(handler, command=command)


@board_app.command("get")
def board_get(board_id: int = typer.Argument(..., help="Board id")) -> None:
    def handler(client: JiraClient) -> Any:
        return client.parse_response(client.request_agile("GET", f"/board/{board_id}"))

    run_api(handler, command="board get")


# --- sprint ---

sprint_app = typer.Typer(help="Jira Software sprints.")
app.add_typer(sprint_app, name="sprint")


@sprint_app.command("list")
def sprint_list(
    board_id: int = typer.Option(..., "--board-id"),
    state: Optional[str] = typer.Option(None, "--state", help="future,active,closed"),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit"),
    start: int = typer.Option(0, "--start"),
    fetch_all: bool = typer.Option(False, "--all"),
    fields: Optional[str] = typer.Option(None, "--fields"),
) -> None:
    command = "sprint list"
    get_ctx().command = command
    params: dict[str, Any] = {}
    if state:
        params["state"] = state
    path = f"/board/{board_id}/sprint"

    if get_ctx().stream or fetch_all:
        client = get_client()
        try:
            if get_ctx().stream:
                stream_agile_values(
                    client,
                    path,
                    params,
                    command=command,
                    limit=limit,
                    start=start,
                    fetch_all=fetch_all,
                    fields=fields,
                )
            else:
                values: list[Any] = []
                current_start = start
                while True:
                    page_params = {**params, "maxResults": str(limit), "startAt": str(current_start)}
                    data = client.parse_response(client.request_agile("GET", path, params=page_params))
                    if not isinstance(data, dict):
                        break
                    page_values = data.get("values", [])
                    if isinstance(page_values, list):
                        values.extend(page_values)
                        current_start += len(page_values)
                    if data.get("isLast") is True or not page_values or len(page_values) < limit:
                        break
                emit(make_ok(project_fields({"values": values, "total": len(values)}, fields), command=command))
        finally:
            client.close()
        raise typer.Exit(EXIT_OK)

    def handler(client: JiraClient) -> Any:
        page_params = {**params, "maxResults": str(limit), "startAt": str(start)}
        data = client.parse_response(client.request_agile("GET", path, params=page_params))
        return project_fields(data, fields)

    run_api(handler, command=command)


@sprint_app.command("get")
def sprint_get(sprint_id: int = typer.Argument(..., help="Sprint id")) -> None:
    def handler(client: JiraClient) -> Any:
        return client.parse_response(client.request_agile("GET", f"/sprint/{sprint_id}"))

    run_api(handler, command="sprint get")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
