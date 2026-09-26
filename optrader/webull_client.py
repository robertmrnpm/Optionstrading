"""Shared Webull OpenAPI client factory + tolerant response parsing helpers.

Uses the official ``webull-openapi-python-sdk``. On first connection Webull creates an
access token that must be approved in the Webull mobile app (2FA). The SDK waits up to
``token_check_duration_seconds`` for that approval and caches the token on disk.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)


class WebullNotConfigured(RuntimeError):
    pass


def make_api_client(app_key: str | None, app_secret: str | None, region: str, token_dir: Path):
    if not app_key or not app_secret:
        raise WebullNotConfigured("WEBULL_APP_KEY / WEBULL_APP_SECRET are not set (see .env.example)")
    try:
        from webull.core.client import ApiClient
    except ImportError as e:  # pragma: no cover
        raise WebullNotConfigured("webull-openapi-python-sdk is not installed: pip install -r requirements.txt") from e
    token_dir.mkdir(parents=True, exist_ok=True)
    client = ApiClient(app_key, app_secret, region, token_check_duration_seconds=300, token_check_interval_seconds=5)
    client.set_token_dir(str(token_dir))
    return client


def response_json(res: Any, what: str) -> Any:
    """Return the JSON body of an SDK response, raising a readable error on failure."""
    status = getattr(res, "status_code", None)
    if status is not None and status != 200:
        body = ""
        try:
            body = res.text[:500]
        except Exception:
            pass
        raise RuntimeError(f"Webull {what} failed: HTTP {status} {body}")
    try:
        return res.json()
    except Exception as e:
        raise RuntimeError(f"Webull {what}: response was not JSON") from e


def pick(d: dict, *keys: str, default: Any = None) -> Any:
    """First present, non-empty value among ``keys`` (Webull field names vary by endpoint/version)."""
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def fnum(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def find_dicts(obj: Any, required: Iterable[str]) -> list[dict]:
    """Walk a JSON structure and return every dict containing all ``required`` keys."""
    req = tuple(required)
    out: list[dict] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if all(k in o for k in req):
                out.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def parse_time(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return datetime.fromtimestamp(x, tz=timezone.utc)
    s = str(v).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_date(v: Any) -> date | None:
    if not v:
        return None
    s = str(v)[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:8], "%Y%m%d").date()
        except ValueError:
            return None
