"""Domain constants for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import re
from typing import Any, Final

# ============================================================
# Duration patterns (regex + multipliers)
# ============================================================
_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d+\.?\d*)([smhd])$")
_DURATION_MULTIPLIERS: Final[dict[str, int]] = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# ============================================================
# Three-state helpers
# ============================================================
_DISABLED_VALUES: Final[frozenset[str]] = frozenset(("disable", "disabled", "off", "false"))

# ============================================================
# HTTP helpers (shared across ops tools that query Prometheus / Alertmanager)
# ============================================================
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset(("http", "https"))

# ============================================================
# Reserved Tenant Config Keys (Python source of truth)
# ============================================================
# Go equivalent: components/threshold-exporter/app/config.go
#   validReservedKeys + validReservedPrefixes — keep in sync.
VALID_RESERVED_KEYS: Final[set[str]] = {
    "_silent_mode", "_severity_dedup", "_namespaces", "_metadata", "_profile",
    "_routing_profile",  # v2.1.0 ADR-007: cross-domain routing profile reference
}
VALID_RESERVED_PREFIXES: Final[tuple[str, ...]] = ("_state_", "_routing")

# ============================================================
# Timing Guardrails
# ============================================================
# Format: (min_seconds, max_seconds, description)
GUARDRAILS: Final[dict[str, tuple[int, int, str]]] = {
    "group_wait": (5, 300, "5s–5m"),
    "group_interval": (5, 300, "5s–5m"),
    "repeat_interval": (60, 259200, "1m–72h"),
}

# Platform defaults (used when tenant doesn't specify)
PLATFORM_DEFAULTS: Final[dict[str, Any]] = {
    "group_by": ["alertname", "tenant"],
    "group_wait": "30s",
    "group_interval": "5m",
    "repeat_interval": "4h",
}

# ============================================================
# Receiver Types
# ============================================================
# Each type maps to: (alertmanager_config_key, required_fields, optional_fields)
RECEIVER_TYPES: Final[dict[str, dict[str, Any]]] = {
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
RECEIVER_URL_FIELDS: Final[dict[str, list[str]]] = {
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
JOB_DB_MAP: Final[dict[str, str]] = {
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

METRIC_PREFIX_DB_MAP: Final[dict[str, str]] = {
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
ONBOARD_HINTS_FILENAME: Final[str] = "onboard-hints.json"
