"""
Shared primitives for market information collectors.

Collectors are deliberately conservative: they return structured data and
warnings instead of raising whenever a single upstream source fails.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


DEFAULT_TIMEOUT_SECONDS = 20
_TRACE_DIR: Optional[Path] = None
_TRACE_LOCK = threading.Lock()
_TRACE_COUNTER = count(1)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def local_today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def configure_http_tracing(trace_dir: Optional[Path]) -> None:
    """Enable or disable raw HTTP response tracing for collector runs."""
    global _TRACE_DIR, _TRACE_COUNTER
    with _TRACE_LOCK:
        _TRACE_DIR = trace_dir
        _TRACE_COUNTER = count(1)
        if _TRACE_DIR is not None:
            (_TRACE_DIR / "responses").mkdir(parents=True, exist_ok=True)


def default_user_agent() -> str:
    sec_user_agent = env("SEC_USER_AGENT")
    if sec_user_agent:
        return sec_user_agent
    return "vurtnec-loom market collector contact@example.com"


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {".", "N/A", "null", "None"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def pct_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100


def absolute_change(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous is None:
        return None
    return current - previous


def compact_error(exc: BaseException) -> str:
    message = f"{type(exc).__name__}: {exc}"
    message = re.sub(
        r"(?i)(api_key|apikey|userid|user_id|token|key)=([^&\s)]+)",
        r"\1=<redacted>",
        message,
    )
    for env_name in [
        "FRED_API_KEY",
        "BEA_API_KEY",
        "EIA_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "GUARDIAN_API_KEY",
    ]:
        secret = env(env_name)
        if secret:
            message = message.replace(secret, "<redacted>")
    return message


def redact_url(url: str) -> str:
    parts = urlsplit(str(url))
    redacted_query = _redact_mapping(dict(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(redacted_query, doseq=True),
            parts.fragment,
        )
    )


def _redact_mapping(values: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for key, value in (values or {}).items():
        if re.search(r"(?i)(api|apikey|api_key|key|token|secret|password)", str(key)):
            redacted[key] = "<redacted>"
        else:
            redacted[key] = value
    return redacted


@dataclass
class DataPoint:
    as_of: str
    source: str
    source_url: str
    category: str
    symbol_or_series: str
    name: str
    value: Any
    previous_value: Any = None
    delta: Optional[float] = None
    delta_pct: Optional[float] = None
    unit: str = ""
    freshness: str = "unknown"
    confidence: str = "unknown"
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NewsItem:
    title: str
    url: str
    source: str
    published_at: str = ""
    category: str = "news"
    summary: str = ""
    source_rank: int = 2
    matched_topics: List[str] = field(default_factory=list)
    asset_relevance: List[str] = field(default_factory=list)
    confidence: str = "unknown"
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CollectorResult:
    name: str
    ok: bool = True
    data_points: List[DataPoint] = field(default_factory=list)
    news_items: List[NewsItem] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def mark_error(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


class HttpClient:
    def __init__(self, user_agent: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout
        self.session = requests.Session()
        # Avoid local proxy/TLS issues observed with some official feeds.
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": user_agent or default_user_agent(),
                "Accept": "application/json,text/csv,application/xml,text/xml,application/rss+xml,text/html;q=0.8,*/*;q=0.5",
            }
        )

    def get(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        started = time.time()
        response = None
        try:
            response = self.session.get(url, **kwargs)
            _trace_http_response(url, kwargs, response, started)
            response.raise_for_status()
            return response
        except Exception as exc:
            if response is None:
                _trace_http_error(url, kwargs, exc, started)
            raise

    def get_json(self, url: str, **kwargs) -> Dict[str, Any]:
        return self.get(url, **kwargs).json()

    def get_text(self, url: str, **kwargs) -> str:
        return self.get(url, **kwargs).text


def _trace_http_response(
    url: str,
    request_kwargs: Dict[str, Any],
    response: requests.Response,
    started: float,
) -> None:
    trace_dir = _TRACE_DIR
    if trace_dir is None:
        return

    with _TRACE_LOCK:
        index = next(_TRACE_COUNTER)
        response_dir = trace_dir / "responses"
        response_dir.mkdir(parents=True, exist_ok=True)
        body_name = f"{index:04d}-{_trace_slug(response.url)}{_trace_extension(response)}"
        body_path = response_dir / body_name
        body_path.write_bytes(response.content)
        _append_trace_manifest(
            trace_dir,
            {
                "id": index,
                "timestamp": utc_now_iso(),
                "method": "GET",
                "url": redact_url(response.url),
                "request_url": redact_url(url),
                "params": _redact_mapping(request_kwargs.get("params") or {}),
                "status_code": response.status_code,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "content_type": response.headers.get("Content-Type", ""),
                "bytes": len(response.content),
                "body_file": f"responses/{body_name}",
            },
        )


def _trace_http_error(
    url: str,
    request_kwargs: Dict[str, Any],
    exc: BaseException,
    started: float,
) -> None:
    trace_dir = _TRACE_DIR
    if trace_dir is None:
        return

    with _TRACE_LOCK:
        index = next(_TRACE_COUNTER)
        _append_trace_manifest(
            trace_dir,
            {
                "id": index,
                "timestamp": utc_now_iso(),
                "method": "GET",
                "url": redact_url(url),
                "params": _redact_mapping(request_kwargs.get("params") or {}),
                "status_code": None,
                "elapsed_ms": round((time.time() - started) * 1000, 2),
                "error": compact_error(exc),
                "body_file": None,
            },
        )


def _append_trace_manifest(trace_dir: Path, item: Dict[str, Any]) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    with (trace_dir / "manifest.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def _trace_slug(url: str) -> str:
    parts = urlsplit(str(url))
    raw = f"{parts.netloc}{parts.path}".strip("/") or "response"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")
    return slug[:80] or "response"


def _trace_extension(response: requests.Response) -> str:
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "json" in content_type:
        return ".json"
    if "csv" in content_type:
        return ".csv"
    if "xml" in content_type or "rss" in content_type:
        return ".xml"
    if "html" in content_type:
        return ".html"
    return ".txt"


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def dataclass_list(items: Iterable[Any]) -> List[Dict[str, Any]]:
    return [asdict(item) for item in items]


def source_status(result: CollectorResult) -> Dict[str, Any]:
    return {
        "ok": result.ok,
        "data_points": len(result.data_points),
        "news_items": len(result.news_items),
        "warnings": result.warnings,
        "errors": result.errors,
        "meta": result.meta,
    }
