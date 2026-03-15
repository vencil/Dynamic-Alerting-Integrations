"""
_lib_python.py — Shared Python utilities for da-tools scripts.

Canonical implementations of:
  - parse_duration_seconds(): Prometheus-style duration → seconds
  - format_duration(): seconds → Prometheus-style duration
  - is_disabled(): three-state disable check
  - load_yaml_file(): YAML loading with encoding + error handling
  - validate_and_clamp(): Timing parameter guardrail enforcement

Domain constants (single source of truth for Python tools):
  - VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES: tenant config reserved keys
  - GUARDRAILS / PLATFORM_DEFAULTS: timing parameter bounds
  - RECEIVER_TYPES / RECEIVER_URL_FIELDS: Alertmanager receiver definitions
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional, Union

import yaml

# ---------------------------------------------------------------------------
# Duration parsing + formatting
# ---------------------------------------------------------------------------
_DURATION_RE = re.compile(r"^(\d+\.?\d*)([smhd])$")
_DURATION_MULTIPLIERS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def detect_cli_lang() -> str:
    """Detect CLI language from environment variables.

    Checks in order: ``DA_LANG``, ``LC_ALL``, ``LANG``.
    Returns ``'zh'`` if any starts with ``zh``, ``'en'`` otherwise.
    """
    for var in ('DA_LANG', 'LC_ALL', 'LANG'):
        val = os.environ.get(var, '')
        if val.startswith('zh'):
            return 'zh'
        if val.startswith('en'):
            return 'en'
    return 'en'


def parse_duration_seconds(value: Union[str, int, float, None]) -> Optional[int]:
    """Parse a Prometheus-style duration string to seconds.

    Accepts: ``5s``, ``30s``, ``1m``, ``5m``, ``1h``, ``4h``, ``72h``,
    ``1d``, or numeric ``int``/``float``.

    Returns:
        Seconds as ``int``, or ``None`` if *value* is invalid.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not value or not isinstance(value, str):
        return None
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        return None
    return int(float(m.group(1)) * _DURATION_MULTIPLIERS[m.group(2)])


def format_duration(seconds: int) -> str:
    """Format seconds back to a Prometheus-compatible duration string.

    Uses the largest whole unit that divides evenly (``h`` → ``m`` → ``s``).

    Note:
        Prometheus/Alertmanager only supports ``s``/``m``/``h`` (not ``d``).
        This function intentionally never produces day units.
    """
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# Three-state helpers
# ---------------------------------------------------------------------------
_DISABLED_VALUES = frozenset(("disable", "disabled", "off", "false"))


def is_disabled(value: Any) -> bool:
    """Check if *value* represents a disabled state.

    Recognises: ``disable``, ``disabled``, ``off``, ``false``
    (case-insensitive, stripped). Consistent with the Go
    ``IsDisabledValue()`` implementation.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------
def load_yaml_file(path: Optional[str], default: Any = None) -> Any:
    """Load a YAML file with UTF-8 encoding and safe parsing.

    Args:
        path: Filesystem path.  Returns *default* if ``None``, empty,
              or non-existent.
        default: Fallback value when the file is missing or empty.

    Returns:
        Parsed YAML data, or *default*.
    """
    if not path or not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if data is not None else default


# ---------------------------------------------------------------------------
# HTTP helpers (shared across ops tools that query Prometheus / Alertmanager)
# ---------------------------------------------------------------------------
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
        # SSRF 防護：僅允許 http/https scheme
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None, f"Unsupported URL scheme: {parsed.scheme}"

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
        # SSRF 防護：僅允許 http/https scheme
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None, f"Unsupported URL scheme: {parsed.scheme}"

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
    import time
    last_error: Optional[Exception] = None

    # SSRF 防護：僅允許 http/https scheme
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")

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


# ---------------------------------------------------------------------------
# Config directory walking
# ---------------------------------------------------------------------------
def iter_yaml_files(
    config_dir: str,
    *,
    skip_reserved: bool = True,
) -> list[tuple[str, str]]:
    """List YAML files in *config_dir*, sorted deterministically.

    Args:
        config_dir: Path to the configuration directory.
        skip_reserved: If ``True`` (default), skip files whose names
                       start with ``_`` or ``.`` (reserved / dotfiles).

    Returns:
        List of ``(filename, full_path)`` tuples, sorted by filename.
    """
    if not config_dir or not os.path.isdir(config_dir):
        return []
    result: list[tuple[str, str]] = []
    for fname in sorted(os.listdir(config_dir)):
        if not (fname.endswith(".yaml") or fname.endswith(".yml")):
            continue
        if skip_reserved and (fname.startswith("_") or fname.startswith(".")):
            continue
        fpath = os.path.join(config_dir, fname)
        if os.path.isfile(fpath):
            result.append((fname, fpath))
    return result


def load_tenant_configs(config_dir: str) -> dict[str, dict[str, Any]]:
    """Load all tenant configurations from a config directory.

    Handles both the ``{tenants: {name: {...}}}`` wrapper format
    (used in ``conf.d/``) and the flat single-tenant format.
    Files starting with ``_`` or ``.`` are skipped.

    Args:
        config_dir: Path to the configuration directory.

    Returns:
        Dict mapping ``tenant_name`` → ``config_dict``.  Empty dict
        on any error or if *config_dir* is missing.
    """
    configs: dict[str, dict[str, Any]] = {}
    for fname, fpath in iter_yaml_files(config_dir):
        raw = load_yaml_file(fpath, default={})
        if not isinstance(raw, dict):
            continue
        if "tenants" in raw and isinstance(raw.get("tenants"), dict):
            for t_name, t_data in raw["tenants"].items():
                if isinstance(t_data, dict):
                    configs[t_name] = t_data
        else:
            tenant = fname.rsplit(".", 1)[0]
            configs[tenant] = raw
    return configs


# ============================================================
# Reserved Tenant Config Keys (Python source of truth)
# ============================================================
# Go equivalent: components/threshold-exporter/app/config.go
#   validReservedKeys + validReservedPrefixes — keep in sync.
VALID_RESERVED_KEYS: set[str] = {
    "_silent_mode", "_severity_dedup", "_namespaces", "_metadata", "_profile",
}
VALID_RESERVED_PREFIXES: tuple[str, ...] = ("_state_", "_routing")


# ============================================================
# Timing Guardrails
# ============================================================
# Format: (min_seconds, max_seconds, description)
GUARDRAILS: dict[str, tuple[int, int, str]] = {
    "group_wait": (5, 300, "5s–5m"),
    "group_interval": (5, 300, "5s–5m"),
    "repeat_interval": (60, 259200, "1m–72h"),
}

# Platform defaults (used when tenant doesn't specify)
PLATFORM_DEFAULTS: dict[str, Any] = {
    "group_by": ["alertname", "tenant"],
    "group_wait": "30s",
    "group_interval": "5m",
    "repeat_interval": "4h",
}


# ============================================================
# Receiver Types
# ============================================================
# Each type maps to: (alertmanager_config_key, required_fields, optional_fields)
RECEIVER_TYPES: dict[str, dict[str, Any]] = {
    "webhook": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved", "http_config"],
    },
    "email": {
        "am_key": "email_configs",
        "required": ["to", "smarthost"],
        "optional": ["from", "auth_username", "auth_password", "require_tls",
                      "html", "text", "headers", "send_resolved"],
    },
    "slack": {
        "am_key": "slack_configs",
        "required": ["api_url"],
        "optional": ["channel", "title", "text", "title_link", "icon_emoji",
                      "send_resolved"],
    },
    "teams": {
        "am_key": "msteams_configs",
        "required": ["webhook_url"],
        "optional": ["title", "text", "send_resolved"],
    },
    "rocketchat": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved"],
        "metadata": ["channel", "username", "icon_url"],  # documented but not passed to AM
    },
    "pagerduty": {
        "am_key": "pagerduty_configs",
        "required": ["service_key"],
        "optional": ["routing_key", "severity", "description", "client",
                      "client_url", "send_resolved"],
    },
}


# ============================================================
# Webhook Domain Allowlist (SSRF prevention)
# ============================================================
# Maps receiver type → list of fields that contain URLs to validate
RECEIVER_URL_FIELDS: dict[str, list[str]] = {
    "webhook":    ["url"],
    "email":      ["smarthost"],      # host:port format
    "slack":      ["api_url"],
    "teams":      ["webhook_url"],
    "rocketchat": ["url"],
    "pagerduty":  [],                 # service_key only, no URL
}


# ============================================================
# DB Type Inference Maps (Job name + Metric prefix → DB type)
# ============================================================
# 用於 blind_spot_discovery 和 onboard_platform 的 DB 類型推斷。
# JOB_DB_MAP: Prometheus job name → DB type（完整比對 + 關鍵字比對）
# METRIC_PREFIX_DB_MAP: Metric 名稱前綴 → DB type
JOB_DB_MAP: dict[str, str] = {
    # MariaDB / MySQL
    "mysql": "mariadb", "mariadb": "mariadb", "mysqld": "mariadb",
    "mysqld_exporter": "mariadb", "mysql_exporter": "mariadb",
    # PostgreSQL
    "postgres": "postgresql", "postgresql": "postgresql", "pg": "postgresql",
    "postgres_exporter": "postgresql",
    # Redis
    "redis": "redis", "redis_exporter": "redis",
    # MongoDB
    "mongo": "mongodb", "mongodb": "mongodb", "mongodb_exporter": "mongodb",
    # Kafka
    "kafka": "kafka", "kafka_exporter": "kafka",
    # RabbitMQ
    "rabbitmq": "rabbitmq", "rabbit": "rabbitmq",
    # Elasticsearch
    "elasticsearch": "elasticsearch", "elastic": "elasticsearch",
    "es": "elasticsearch",
    # Oracle
    "oracle": "oracle", "oracledb": "oracle", "oracledb_exporter": "oracle",
    # ClickHouse
    "clickhouse": "clickhouse",
    # DB2
    "db2": "db2",
}

METRIC_PREFIX_DB_MAP: dict[str, str] = {
    "mysql_": "mariadb", "mariadb_": "mariadb",
    "pg_": "postgresql", "postgres_": "postgresql",
    "redis_": "redis",
    "mongo_": "mongodb", "mongodb_": "mongodb",
    "kafka_": "kafka",
    "rabbitmq_": "rabbitmq", "rabbit_": "rabbitmq",
    "elasticsearch_": "elasticsearch", "es_": "elasticsearch",
    "oracle_": "oracle",
    "clickhouse_": "clickhouse",
    "db2_": "db2",
}


# ============================================================
# Onboard Hints (pipeline state between onboard → scaffold)
# ============================================================
ONBOARD_HINTS_FILENAME: str = "onboard-hints.json"


def write_onboard_hints(output_dir: str, hints: dict[str, Any]) -> str:
    """Write onboard hints JSON for scaffold consumption.

    Args:
        output_dir: Directory to write ``onboard-hints.json`` into.
        hints: Data dict (tenants, db_types, routing_hints, …).

    Returns:
        Absolute path to the written file.
    """
    import json
    path = os.path.join(output_dir, ONBOARD_HINTS_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(hints, f, indent=2, ensure_ascii=False)
    os.chmod(path, 0o600)
    return path


def read_onboard_hints(path: Optional[str]) -> Optional[dict[str, Any]]:
    """Read onboard hints JSON.

    Returns:
        Parsed dict, or ``None`` if file is missing / unreadable.
    """
    import json
    if not path or not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def validate_and_clamp(
    param: str,
    value: Union[str, int, float],
    tenant: str,
) -> tuple[Union[str, int, float], list[str]]:
    """Validate a timing parameter against guardrails and clamp if needed.

    Args:
        param: Parameter name (``group_wait``, ``group_interval``,
               ``repeat_interval``).
        value: Duration string (e.g. ``"30s"``) or numeric seconds.
        tenant: Tenant identifier (used in warning messages).

    Returns:
        A 2-tuple ``(clamped_value, warnings)`` where *warnings* is a
        list of human-readable strings (empty if within bounds).
    """
    warnings: list[str] = []

    if param not in GUARDRAILS:
        return value, warnings

    min_sec, max_sec, desc = GUARDRAILS[param]
    seconds = parse_duration_seconds(value)

    if seconds is None:
        warnings.append(f"  WARN: {tenant}: invalid {param} '{value}', using platform default")
        return PLATFORM_DEFAULTS.get(param, value), warnings

    if seconds < min_sec:
        clamped = format_duration(min_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' below minimum ({desc}), clamped to {clamped}")
        return clamped, warnings

    if seconds > max_sec:
        clamped = format_duration(max_sec)
        warnings.append(f"  WARN: {tenant}: {param} '{value}' above maximum ({desc}), clamped to {clamped}")
        return clamped, warnings

    return value, warnings
