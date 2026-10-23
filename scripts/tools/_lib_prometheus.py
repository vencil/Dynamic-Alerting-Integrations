"""HTTP and Prometheus query helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from _lib_constants import _ALLOWED_SCHEMES
# No import cycle: _lib_exitcodes imports only stdlib + _lib_compat (#1641).
from _lib_exitcodes import CallerError


class QueryError(CallerError):
    """A query a tool asked for and did not get a usable answer to (#3133).

    Raised for every way the source can fail to answer: no connection, an
    HTTP error, ``status`` other than ``success``, and a ``success`` body
    whose shape is not what the query returns (``malformed <source>
    response: …``). Each of these used to come back as an empty result
    somewhere, which reads as "no data" — a measurement that never happened.

    It is a named caller error: uncaught, it ends the process at rc 2 with
    one ``ERROR: <source> query failed: <message>`` line on stderr and, in
    ``--json`` mode, a ``caller_error`` envelope whose ``reason`` is
    ``<source>_query_error`` (``prometheus_query_error`` /
    ``alertmanager_query_error``). A tool that tolerates a failed query
    must say so with an explicit ``except QueryError``.

    The message is the error text as received (or the shape defect);
    *source* (``"Prometheus"`` / ``"Alertmanager"``) names who failed.
    """

    reason = "prometheus_query_error"

    def __init__(self, message: str, reason: Optional[str] = None, *,
                 source: str = "Prometheus") -> None:
        super().__init__(message, reason if reason is not None
                         else f"{source.lower()}_query_error")
        self.source = source

    @property
    def error_line(self) -> str:
        return f"ERROR: {self.source} query failed: {self}"


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
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:  # nosec B310  #scheme validated by _validate_url_scheme upstream
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
            with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:  # nosec B310  #scheme validated by _validate_url_scheme upstream
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


def probe_health(
    url: str,
    *,
    timeout: int = 10,
) -> tuple[Optional[str], Optional[str]]:
    """Probe a health/readiness endpoint with a plain GET.

    Consolidates the hand-rolled ``urllib.request.urlopen(Request(url))``
    liveness probes (da-tools ROI r3 W2): federation_check ×2 (`/-/healthy`),
    byo_check ×2 (`/-/healthy` + Alertmanager `/-/ready`), shadow_verify ×1
    (`/-/healthy`, reads the body). NOT Prometheus-specific — the caller
    composes the full probe URL (base + path), so any HTTP health endpoint
    works.

    Unlike the pre-consolidation call sites, the URL scheme is validated
    first (:func:`_validate_url_scheme`) — the hand-rolled probes had NO
    scheme check, an SSRF-protection gap relative to every other HTTP
    helper in this lib.

    Args:
        url: FULL probe URL (e.g. ``f"{prom_url}/-/healthy"``).
        timeout: Socket timeout in seconds (default 10 — all five
            consolidated sites used 10).

    Returns:
        ``(body_str, None)`` on success — decoded response body (UTF-8,
        ``errors="replace"`` so a probe that answered 2xx never fails on
        decode; may be ``""``). Reachability-only callers ignore the body.
        ``(None, err_str)`` on failure — ``str(exception)``, matching the
        detail strings the hand-rolled sites produced, or the scheme error.
    """
    try:
        scheme_err = _validate_url_scheme(url)
        if scheme_err:
            return None, scheme_err
        req = urllib.request.Request(url)  # nosec B310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310  #scheme validated by _validate_url_scheme upstream
            return resp.read().decode("utf-8", errors="replace"), None
    except (urllib.error.URLError, ValueError, OSError) as exc:
        return None, str(exc)


def query_prometheus_instant(
    prom_url: str,
    promql: str,
    *,
    timeout: int = 10,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Execute a Prometheus instant query and return (results, error).

    Args:
        prom_url: Prometheus base URL.
        promql: PromQL query string (percent-encoded here — callers must
            NOT pre-encode).
        timeout: Socket timeout in seconds (default 10 — same as
            :func:`http_get_json`, so the additive keyword changed no
            behaviour for existing callers).

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
    data, err = http_get_json(full_url, timeout=timeout)
    # Guard non-dict JSON bodies ("null" / "[]" / "0" from a misrouted
    # endpoint): http_get_json parses them fine, but .get() would raise.
    if err or not isinstance(data, dict):
        return None, err or "non-JSON-object response"
    if data.get("status") != "success":
        return None, data.get("error", "Unknown Prometheus error")
    return data.get("data", {}).get("result", []), None


def query_prometheus_range(
    prom_url: str,
    promql: str,
    start: float,
    end: float,
    step: Any,
    *,
    timeout: int = 30,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """Execute a Prometheus range query (``/api/v1/query_range``).

    Consolidates the hand-rolled fetch cores of backtest_threshold /
    alert_quality / cardinality_forecasting (da-tools ROI r3 W1) so the
    percent-encoding of the PromQL — which carries ``{``, ``}``, ``"`` and
    often spaces — lives in exactly one place (#1112 InvalidURL bug-class).

    Args:
        prom_url: Prometheus base URL.
        promql: PromQL query string (percent-encoded here — callers must
            NOT pre-encode).
        start: Range start as a unix timestamp (int/float; formatted with
            ``:.0f``, matching all pre-consolidation call sites).
        end: Range end as a unix timestamp (same formatting).
        step: Query resolution step — Prometheus duration string (``"5m"``)
            or plain seconds (``60``); passed through as-is.
        timeout: Socket timeout in seconds (default 30 — range queries scan
            more data than instant ones; two of the three consolidated sites
            already used 30).

    Returns:
        (list[dict], None) on success — each dict has 'metric' and 'values'
        (matrix) keys.
        (None, str) on error — error message string (mirrors
        :func:`query_prometheus_instant`, including the literal
        ``"Unknown Prometheus error"`` fallback).
    """
    data, err = http_get_json(_range_url(prom_url, promql, start, end, step),
                              timeout=timeout)
    # Guard non-dict JSON bodies ("null" / "[]" / "0" from a misrouted
    # endpoint): http_get_json parses them fine, but .get() would raise.
    if err or not isinstance(data, dict):
        return None, err or "non-JSON-object response"
    if data.get("status") != "success":
        return None, data.get("error", "Unknown Prometheus error")
    return data.get("data", {}).get("result", []), None


def _range_url(prom_url: str, promql: str, start: float, end: float, step: Any) -> str:
    params: str = urllib.parse.urlencode({
        "query": promql,
        "start": f"{start:.0f}",
        "end": f"{end:.0f}",
        "step": step,
    })
    return f"{prom_url}/api/v1/query_range?{params}"


def _malformed(source: str, defect: str) -> QueryError:
    return QueryError(f"malformed {source} response: {defect}", source=source)


def sample_value(raw: Any) -> float:
    """The float a Prometheus sample holds — ``+Inf`` / ``-Inf`` / ``NaN``
    included. The API writes every sample value as a JSON string; a JSON
    number is read too, as the tools read it before (refusing it would
    change their exit code, which is not decided). Anything ``float()``
    cannot read — and a boolean — raises ``ValueError``, which
    ``_matrix_rows`` turns into a ``QueryError`` naming the sample. The one
    reading of a sample (#3213): tools do not call ``float()`` / ``int()`` on
    one themselves."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"{raw!r:.40} is not a sample value")
    return float(raw)


def row_samples(row: dict[str, Any]) -> list[tuple[float, float]]:
    """``(timestamp, value)`` of every float sample of a ``_matrix_rows``
    row, parsed once here (non-finite values kept: whether ``+Inf`` is a
    sample or a gap is the tool's question). A histogram-only row has none."""
    return [(float(ts), sample_value(val)) for ts, val in row.get("values", [])]


def finite_row_samples(row: dict[str, Any]) -> tuple[list[tuple[float, float]], int]:
    """``row_samples`` without the non-finite ones, and how many were
    dropped — for a tool that needs a count or a regression (#3213), which
    reports the number dropped rather than crashing or hiding them."""
    samples = row_samples(row)
    kept = [(ts, v) for ts, v in samples if math.isfinite(v)]
    return kept, len(samples) - len(kept)


def _success_result(body: Any, result_type: str) -> Any:
    """``data.result`` of a ``success`` body whose ``resultType`` is
    *result_type*, or ``QueryError`` — the part every Prometheus answer
    shares (the error text as Prometheus wrote it, or the defect named)."""
    if not isinstance(body, dict):
        raise _malformed("Prometheus", "body is not a JSON object")
    if body.get("status") != "success":
        raise QueryError(str(body.get("error", "Unknown Prometheus error")))
    data = body.get("data")
    if not isinstance(data, dict):
        raise _malformed("Prometheus", "'data' is missing or not an object")
    if data.get("resultType") != result_type:
        raise _malformed("Prometheus",
                         f"resultType is {data.get('resultType')!r}, expected {result_type!r}")
    return data.get("result")


def _vector_rows(body: Any) -> list[dict[str, Any]]:
    """The rows of an instant ``query`` answer, or ``QueryError``.

    ``resultType: vector`` and ``result`` a list whose every row is an
    object with ``value`` (a ``[timestamp, sample]`` pair ``sample_value``
    reads) and/or ``histogram`` (a native-histogram sample, not read as a
    float), and, when present, a ``metric`` object (#3133). One row that is
    not counts for the whole answer, as in ``_matrix_rows``.
    """
    rows = _success_result(body, "vector")
    if not isinstance(rows, list):
        raise _malformed("Prometheus", "'data.result' is missing or not a list")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise _malformed("Prometheus", f"result[{i}] is not an object")
        if not isinstance(row.get("metric", {}), dict):
            raise _malformed("Prometheus", f"result[{i}].metric is not an object")
        if "value" not in row and "histogram" not in row:
            raise _malformed("Prometheus",
                             f"result[{i}] has neither 'value' nor 'histogram'")
        if "value" in row:
            sample = row["value"]
            try:
                if not (isinstance(sample, list) and len(sample) == 2):
                    raise ValueError
                sample_value(sample[1])  # the value only, as _matrix_rows
            except (TypeError, ValueError):
                raise _malformed("Prometheus",
                                 f"result[{i}].value is {sample!r:.40}, "
                                 "expected [timestamp, number]") from None
    return rows


def vector_sample(row: dict[str, Any]) -> Optional[float]:
    """The float sample of a ``_vector_rows`` row (non-finite kept), or
    None for a histogram-only row."""
    return sample_value(row["value"][1]) if "value" in row else None


def vector_count(rows: list[dict[str, Any]]) -> Optional[int]:
    """The number a ``count(...)`` instant query answered — the sample of the
    first ``_vector_rows`` row — or None when Prometheus answered with an
    empty vector (``count()`` over nothing).

    Raises ``QueryError`` when that sample is not a finite number (a
    histogram row, ``NaN``, ``±Inf``): no count is one, and ``int()`` on it
    used to crash the tool (#3133). The one reading of a count: tools do not
    call ``int(float(...))`` on a sample themselves."""
    if not rows:
        return None
    value = vector_sample(rows[0])
    if value is None or not math.isfinite(value):
        raise _malformed("Prometheus", f"result[0] is {rows[0].get('value', rows[0])!r:.40}, "
                                       "expected a finite count")
    return int(value)


def _matrix_rows(body: Any) -> list[dict[str, Any]]:
    """The rows of a ``query_range`` answer, or ``QueryError``.

    A ``success`` body must hold ``data`` (an object) with ``resultType:
    matrix`` and ``result`` a list whose every row is an object with a
    ``values`` and/or ``histograms`` list of ``[timestamp, sample]`` pairs
    (and, when present, a ``metric`` object); every float sample (``values``)
    is a number ``sample_value`` reads (#3213). One row that is not counts for the whole answer:
    a tool reading the rows that are fine would report the others as
    absent (#3074).
    """
    rows = _success_result(body, "matrix")
    if not isinstance(rows, list):
        raise _malformed("Prometheus", "'data.result' is missing or not a list")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise _malformed("Prometheus", f"result[{i}] is not an object")
        if not isinstance(row.get("metric", {}), dict):
            raise _malformed("Prometheus", f"result[{i}].metric is not an object")
        # A matrix row carries float samples (`values`), native-histogram
        # samples (`histograms`), or both: the API omits the one it has none
        # of. The tools read only `values` and skip a row without it, so a
        # histogram-only row is a real, answered row — not a defect.
        present = [k for k in ("values", "histograms") if k in row]
        if not present:
            raise _malformed("Prometheus",
                             f"result[{i}] has neither 'values' nor 'histograms'")
        for key in present:
            samples = row[key]
            if not isinstance(samples, list):
                raise _malformed("Prometheus", f"result[{i}].{key} is not a list")
            for j, sample in enumerate(samples):
                if not (isinstance(sample, list) and len(sample) == 2):
                    raise _malformed("Prometheus",
                                     f"result[{i}].{key} holds {sample!r:.40}, "
                                     "expected [timestamp, sample]")
                if key != "values":
                    continue
                # Only the value: an unreadable timestamp is not refused here
                # (backtest never reads it; refusing would change its rc).
                try:
                    sample_value(sample[1])
                except (TypeError, ValueError):
                    raise _malformed("Prometheus",
                                     f"result[{i}].values[{j}] is {sample!r:.40}, "
                                     "expected [timestamp, number]") from None
    return rows


def prometheus_instant(
    prom_url: str,
    promql: str,
    *,
    timeout: int = 10,
    result_type: str = "vector",
) -> list[dict[str, Any]]:
    """Execute a Prometheus instant query; return its rows or raise.

    Same request as :func:`query_prometheus_instant` (percent-encoded once
    here), but a query that was not answered is never a value (#3133).
    *result_type* is the answer the PromQL asks for: ``vector``, or
    ``matrix`` for a range-vector selector (``x[7d]``) evaluated at one
    instant — then the rows are checked as :func:`_matrix_rows` checks them.

    Returns:
        The vector rows (``metric``, ``value``; read the sample with
        :func:`vector_sample`), or the matrix rows (read with
        :func:`row_samples`). ``[]`` only when Prometheus answered with an
        empty result.

    Raises:
        QueryError: no connection, an HTTP error, ``status`` other than
            ``success``, or a ``success`` body that is not a vector of
            readable samples (see :func:`_vector_rows`).
    """
    readers = {"vector": _vector_rows, "matrix": _matrix_rows}
    if result_type not in readers:
        raise ValueError(f"result_type {result_type!r}: expected one of {sorted(readers)}")
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    data, err = http_get_json(url, timeout=timeout)
    if err:
        raise QueryError(err)
    return readers[result_type](data)


def instant_or_error(
    prom_url: str,
    promql: str,
    *,
    timeout: int = 10,
) -> tuple[Optional[list[dict[str, Any]]], Optional[str]]:
    """:func:`prometheus_instant` for a check tool whose judges take a result
    *and its error* (byo-check, federation-check, shadow-verify): ``(rows,
    None)``, or ``(None, why)`` when the query was not answered.

    The answer is checked exactly as :func:`prometheus_instant` checks it —
    unlike :func:`query_prometheus_instant`, which hands back any
    ``success`` body's ``result`` unread. The error is a value here only
    because each of those tools reports it as a failed check of its own;
    every ``err`` it returns must reach a check (#3133: a discarded one is
    the "not measured reads as nothing there" bug)."""
    try:
        return prometheus_instant(prom_url, promql, timeout=timeout), None
    except QueryError as exc:
        return None, str(exc)


def prometheus_range(
    prom_url: str,
    promql: str,
    start: float,
    end: float,
    step: Any,
    *,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """Execute a Prometheus range query; return its rows or raise.

    Same request as :func:`query_prometheus_range` (one percent-encoding
    point, #1112), but a query that was not answered is never a value.

    Returns:
        The matrix rows — each a dict with ``values`` (a list of
        ``[timestamp, value]``) and/or ``histograms`` (native-histogram
        samples), and usually ``metric``. A caller reading float samples
        uses ``row.get("values", [])``: a histogram-only row has none. ``[]`` only when
        Prometheus answered with an empty matrix.

    Raises:
        QueryError: no connection, an HTTP error, ``status`` other than
            ``success`` (the message is Prometheus's ``error``), or a
            ``success`` body of the wrong shape (``malformed Prometheus
            response: …``, see :func:`_matrix_rows`).
    """
    data, err = http_get_json(_range_url(prom_url, promql, start, end, step),
                              timeout=timeout)
    if err:
        raise QueryError(err)
    return _matrix_rows(data)


def alertmanager_alerts(
    am_url: str,
    *,
    filters: tuple[str, ...] = (),
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """Alertmanager's ``/api/v2/alerts``; return the alerts or raise.

    Args:
        am_url: Alertmanager base URL.
        filters: label matchers (``'tenant=db-a'``), each sent as one
            ``filter`` parameter, percent-encoded here.
        timeout: socket timeout in seconds.

    Returns:
        The alerts, each a dict (``labels``, when present, an object).
        ``[]`` only when Alertmanager answered with an empty list.

    Raises:
        QueryError: (``source="Alertmanager"``, reason
            ``alertmanager_query_error``) no connection, an HTTP error, or
            a body that is not a list of alert objects — an Alertmanager
            that did not answer is not "no alerts" (#3073).
    """
    url = f"{am_url}/api/v2/alerts"
    if filters:
        url += "?" + urllib.parse.urlencode([("filter", f) for f in filters])
    data, err = http_get_json(url, timeout=timeout)
    if err:
        raise QueryError(err, source="Alertmanager")
    if not isinstance(data, list):
        raise _malformed("Alertmanager", "body is not a JSON array")
    for i, alert in enumerate(data):
        if not isinstance(alert, dict):
            raise _malformed("Alertmanager", f"alert[{i}] is not an object")
        # A label value is a string in Alertmanager's API; anything else is
        # not an answer a tool can match on (#3133, review of #3196).
        _string_labels("Alertmanager", f"alert[{i}]", alert.get("labels", {}))
    return data


def alertmanager_silences(am_url: str, *, timeout: int = 15) -> list[dict[str, Any]]:
    """Alertmanager's ``/api/v2/silences``; return the silences or raise.

    Returns:
        The silences, each a dict whose ``status``, when present, is an
        object with ``state`` (when present) a string. ``[]`` only when
        Alertmanager answered with an empty list.

    Raises:
        QueryError: (``source="Alertmanager"``) no connection, an HTTP error,
            or a body that is not a list of silence objects — silences that
            could not be read are not "no silences" (#3133).
    """
    data, err = http_get_json(f"{am_url}/api/v2/silences", timeout=timeout)
    if err:
        raise QueryError(err, source="Alertmanager")
    if not isinstance(data, list):
        raise _malformed("Alertmanager", "body is not a JSON array")
    for i, silence in enumerate(data):
        if not isinstance(silence, dict):
            raise _malformed("Alertmanager", f"silence[{i}] is not an object")
        status = silence.get("status", {})
        if not isinstance(status, dict):
            raise _malformed("Alertmanager", f"silence[{i}].status is not an object")
        if not isinstance(status.get("state", ""), str):
            raise _malformed("Alertmanager", f"silence[{i}].status.state is not a string")
    return data


def _string_labels(source: str, where: str, labels: Any) -> None:
    """``labels`` must be an object of string values — what a tool matches on."""
    if not isinstance(labels, dict):
        raise _malformed(source, f"{where}.labels is not an object")
    bad = next((k for k, v in labels.items() if not isinstance(v, str)), None)
    if bad is not None:
        raise _malformed(source, f"{where}.labels.{bad} is not a string")


def _api_list(prom_url: str, path: str, key: str, item: str, timeout: int) -> list[dict[str, Any]]:
    """``data.<key>`` of a ``success`` answer from ``<prom_url><path>``: a list
    of objects whose ``labels`` (when present) are string-valued, or
    ``QueryError``."""
    data, err = http_get_json(f"{prom_url}{path}", timeout=timeout)
    if err:
        raise QueryError(err)
    if not isinstance(data, dict):
        raise _malformed("Prometheus", "body is not a JSON object")
    if data.get("status") != "success":
        raise QueryError(str(data.get("error", "Unknown Prometheus error")))
    body = data.get("data")
    if not isinstance(body, dict):
        raise _malformed("Prometheus", "'data' is missing or not an object")
    rows = body.get(key)
    if not isinstance(rows, list):
        raise _malformed("Prometheus", f"'data.{key}' is missing or not a list")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise _malformed("Prometheus", f"{item}[{i}] is not an object")
        _string_labels("Prometheus", f"{item}[{i}]", row.get("labels", {}))
    return rows


def prometheus_alerts(prom_url: str, *, timeout: int = 10) -> list[dict[str, Any]]:
    """Prometheus's ``/api/v1/alerts``; return its alerts or raise.

    Returns:
        ``data.alerts``: objects whose ``labels``, when present, are
        string-valued. ``[]`` only when Prometheus answered with none.

    Raises:
        QueryError: no connection, an HTTP error, ``status`` other than
            ``success``, or a body that is not a list of alert objects — a
            Prometheus that did not answer is not "no alert firing" (#3133).
    """
    return _api_list(prom_url, "/api/v1/alerts", "alerts", "alert", timeout)


def prometheus_targets(prom_url: str, *, state: str = "active",
                       timeout: int = 10) -> list[dict[str, Any]]:
    """Prometheus's ``/api/v1/targets?state=<state>``; return the targets or raise.

    Returns:
        ``data.activeTargets``: objects whose ``labels``, when present, are
        string-valued. ``[]`` only when Prometheus answered with none.

    Raises:
        QueryError: as :func:`prometheus_alerts` — targets that could not be
            read are not "no target scraped" (#3133).
    """
    path = "/api/v1/targets?" + urllib.parse.urlencode({"state": state})
    return _api_list(prom_url, path, "activeTargets", "target", timeout)


def alertmanager_status(am_url: str, *, timeout: int = 10) -> dict[str, Any]:
    """Alertmanager's ``/api/v2/status``; return it or raise.

    Returns:
        The status object; its ``config``, when present, is an object whose
        ``original`` (the loaded config's YAML), when present, is a string.

    Raises:
        QueryError: (``source="Alertmanager"``) no connection, an HTTP error,
            or a body that is not such an object (#3133).
    """
    data, err = http_get_json(f"{am_url}/api/v2/status", timeout=timeout)
    if err:
        raise QueryError(err, source="Alertmanager")
    if not isinstance(data, dict):
        raise _malformed("Alertmanager", "body is not a JSON object")
    config = data.get("config", {})
    if not isinstance(config, dict):
        raise _malformed("Alertmanager", "'config' is not an object")
    if not isinstance(config.get("original", ""), str):
        raise _malformed("Alertmanager", "'config.original' is not a string")
    return data
