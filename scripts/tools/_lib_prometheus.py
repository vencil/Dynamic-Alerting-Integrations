"""HTTP and Prometheus query helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from _lib_constants import _ALLOWED_SCHEMES


def _validate_url_scheme(url: str) -> Optional[str]:
    """Validate URL scheme for SSRF prevention.

    Returns:
        ``None`` if the scheme is allowed, or an error message string
        if the scheme is disallowed.
    """
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in _ALLOWED_SCHEMES:
        return f"Unsupported URL scheme: {scheme}"
    return None


def http_get_json(
    url: str,
    *,
    timeout: int = 10,
    headers: Optional[dict[str, str]] = None,
) -> tuple[Optional[dict], Optional[str]]:
    """HTTP GET with JSON response parsing.

    A thin wrapper around :mod:`urllib.request` that covers the common
    pattern used by 11+ ops tools: build request, open with timeout,
    decode JSON, catch network errors.

    Args:
        url: Full URL to fetch (e.g. ``http://localhost:9090/api/v1/query``).
        timeout: Socket timeout in seconds (default 10).
        headers: Optional extra headers to set on the request.

    Returns:
        ``(data_dict, None)`` on success, or ``(None, error_message)`` on
        failure (network error, JSON decode error, etc.).
    """
    try:
        scheme_err = _validate_url_scheme(url)
        if scheme_err:
            return None, scheme_err

        req = urllib.request.Request(url)  # nosec B310
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = resp.read().decode("utf-8")
            data = json.loads(body) if body else {}
            return data, None
    except (urllib.error.URLError, urllib.error.HTTPError,
            ValueError, OSError) as exc:
        return None, str(exc)


def http_post_json(
    url: str,
    payload: Any = None,
    *,
    timeout: int = 10,
    headers: Optional[dict[str, str]] = None,
    method: str = "POST",
) -> tuple[Optional[dict], Optional[str]]:
    """HTTP POST (or custom method) with JSON request/response.

    Args:
        url: Full URL to send the request to.
        payload: Python object to JSON-encode as the request body.
            If ``None``, sends an empty body.
        timeout: Socket timeout in seconds (default 10).
        headers: Optional extra headers.
        method: HTTP method (default ``POST``).

    Returns:
        ``(response_dict, None)`` on success, or ``(None, error_message)``
        on failure.
    """
    try:
        scheme_err = _validate_url_scheme(url)
        if scheme_err:
            return None, scheme_err

        req = urllib.request.Request(url, method=method)  # nosec B310
        req.add_header("Content-Type", "application/json")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return (json.loads(body) if body else {}), None
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.reason}"
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return None, str(exc)


def http_request_with_retry(
    url: str,
    *,
    method: str = "GET",
    payload: Any = None,
    timeout: int = 10,
    max_retries: int = 3,
) -> dict:
    """HTTP request with exponential backoff retry（5xx / 連線錯誤自動重試）。

    與 :func:`http_post_json` 不同，此函式在最終失敗時 **raise** 而非回傳
    ``(None, error)``，適用於必須成功的 API 呼叫（如 Alertmanager silence 管理）。

    重試策略：
    - 4xx 錯誤：不重試，立即 raise
    - 5xx / 連線錯誤：最多重試 *max_retries* 次，間隔 1s → 2s → 4s

    Args:
        url: 完整 URL。
        method: HTTP method（預設 ``GET``）。
        payload: JSON-serializable payload（``None`` 表示無 body）。
        timeout: Socket timeout 秒數。
        max_retries: 最大重試次數（預設 3）。

    Returns:
        解析後的 JSON dict。

    Raises:
        urllib.error.HTTPError: 4xx 錯誤或重試耗盡後的 5xx 錯誤。
        urllib.error.URLError: 連線錯誤且重試耗盡。
    """
    last_error: Optional[Exception] = None

    scheme_err = _validate_url_scheme(url)
    if scheme_err:
        raise ValueError(scheme_err)

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, method=method)  # nosec B310
            req.add_header("Content-Type", "application/json")
            data = None
            if payload is not None:
                data = json.dumps(payload).encode("utf-8")
            with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise  # 4xx: 不重試
            last_error = exc
        except (urllib.error.URLError, OSError) as exc:
            last_error = exc

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # 1s, 2s, 4s

    raise last_error  # type: ignore[misc]


def query_prometheus_instant(
    prom_url: str,
    promql: str,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Execute a Prometheus instant query and return (results, error).

    Returns:
        (list[dict], None) on success — each dict has 'metric' and 'value' keys.
        (None, str) on error — error message string.

    Example:
        results, err = query_prometheus_instant("http://localhost:9090", "up")
        if err:
            print(f"Query failed: {err}")
        else:
            for r in results:
                print(r["metric"], r["value"][1])
    """
    url: str = f"{prom_url}/api/v1/query"
    params: str = urllib.parse.urlencode({"query": promql})
    full_url: str = f"{url}?{params}"
    data, err = http_get_json(full_url)
    if err:
        return None, err
    if data.get("status") != "success":
        return None, data.get("error", "Unknown Prometheus error")
    return data.get("data", {}).get("result", []), None
