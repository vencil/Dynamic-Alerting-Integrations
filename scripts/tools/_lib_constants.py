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
# Published documentation (#1447)
# ============================================================
# ⛔ Anything these tools PRINT for a human to act on has to name something
# that human can open.  The audience is routinely a customer whose repository
# holds `conf.d/` and whatever `da-tools init` wrote — a `docs/...` or
# `scripts/...` path is a dead reference over there, and the tool shipped as
# a container image so there is no repo checkout to fall back on.
#
# `mkdocs.yml` builds every page under `docs/` (only `exclude_docs` holds any
# back) and leaves `use_directory_urls` at its default, so a repo-relative
# `docs/a/b.md` is served at `<base>a/b/`.
DOCS_SITE_BASE: Final[str] = (
    "https://vencil.github.io/Dynamic-Alerting-Integrations/"
)
# How to obtain `da-tools` itself. Printed by tools whose output tells the
# reader to run another `da-tools` subcommand.
DOCS_INSTALL_URL: Final[str] = (
    DOCS_SITE_BASE + "migration-toolkit-installation/"
)

# ============================================================
# Reserved Tenant Config Keys
# ============================================================
# The top-level `_` keys are listed once, in
# components/threshold-exporter/app/pkg/config/reserved_keys.json (ADR-038);
# the block below is generated from it, as are Go's copies
# (pkg/config/reserved_keys_gen.go) and the two schemas' `_` keys.
#
# VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES: the reserved tenant keys
# (read in a tenant's own config) and the prefixes that reserve every key
# under them. TENANT_ROUTING_KEYS / PLATFORM_ROUTING_KEYS: the `_routing`
# keys the route generator reads in a tenant's config / at the top level of
# a root conf.d `_` file (#2983). TOP_LEVEL_READ_ELSEWHERE: the `_` keys
# another reader takes from the top level of a root `_` file, so
# validate_config never reports them (#2386; pinned beside da-guard's by
# tests/shared/defaults_wrapper_matrix.json). The policy readers use it too,
# to keep platform-level keys out of a tenant's reserved keys.
# RETIRED_RESERVED_KEYS: keys nothing reads any more, with the hint printed.
# BEGIN GENERATED reserved keys — scripts/tools/dx/sync_schema.py --update writes this
# block from components/threshold-exporter/app/pkg/config/reserved_keys.json. DO NOT EDIT.
VALID_RESERVED_KEYS: Final[set[str]] = {
    "_custom_alerts",
    "_metadata",
    "_namespaces",
    "_profile",
    "_routing",
    "_routing_profile",
    "_severity_dedup",
    "_silent_mode",
}
VALID_RESERVED_PREFIXES: Final[tuple[str, ...]] = ("_state_", "_routing")
TENANT_ROUTING_KEYS: Final[frozenset[str]] = frozenset({
    "_routing",
    "_routing_profile",
})
PLATFORM_ROUTING_KEYS: Final[frozenset[str]] = frozenset({
    "_routing_defaults",
    "_routing_enforced",
})
TOP_LEVEL_READ_ELSEWHERE: Final[frozenset[str]] = frozenset({
    "_policies",
    "_routing_defaults",
    "_routing_enforced",
})
RETIRED_RESERVED_KEYS: Final[dict[str, str]] = {
    "_routing_receiver_type":
        "no longer read: set the receiver type as `_routing.receiver.type`",
}
# Each key's and prefix's readers, reader -> the layers it reads it in.
RESERVED_KEY_READERS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "_custom_alerts": {
        "exporter": ("tenant", "platform_tenants", "profile"),
        "custom_alert_compiler": ("tenant", "platform_tenants", "profile"),
    },
    "_metadata": {
        "exporter": ("tenant", "platform_tenants", "profile"),
    },
    "_namespaces": {
        "annotation": ("tenant", "platform_tenants"),
    },
    "_policies": {
        "policy_engine": ("platform_top",),
    },
    "_profile": {
        "exporter": ("tenant", "platform_tenants"),
    },
    "_routing": {
        "generator": ("tenant", "platform_tenants"),
    },
    "_routing_defaults": {
        "generator": ("platform_top",),
    },
    "_routing_enforced": {
        "generator": ("platform_top",),
    },
    "_routing_profile": {
        "generator": ("tenant", "platform_tenants"),
    },
    "_severity_dedup": {
        "exporter": ("tenant", "platform_tenants", "subtree_defaults"),
        "generator": ("tenant", "platform_tenants"),
    },
    "_silent_mode": {
        "exporter": ("tenant", "platform_tenants", "profile"),
    },
}
RESERVED_PREFIX_READERS: Final[tuple[tuple[str, dict[str, tuple[str, ...]]], ...]] = (
    ("_state_", {
        "exporter": ("tenant", "platform_tenants", "profile", "subtree_defaults"),
    }),
    ("_routing", {}),
)
# END GENERATED reserved keys

# ============================================================
# Custom alerts
# ============================================================
# Per-tenant cap on the custom-alert rows a tenant's OWN `_custom_alerts`
# serves — the list written in its tenant file or in a root platform file's
# `tenants:` entry; one a profile gives it is a shape the profile's tenants
# share and is not counted (#3042). A row is one `user_threshold` series the
# exporter serves: a switched-off recipe serves none, an slo_burn_rate two.
# The same number as Go's pkg/config.MaxCustomRecipesDefault (tenant-api's
# write check), pinned by tests/ops/test_validate_config_custom_alert_cap.py.
MAX_CUSTOM_RECIPES: Final[int] = 20

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
# Each type maps to: am_key (Alertmanager config key), required (every field
# must be set), optional, and — where a type needs EXACTLY ONE of a field group —
# exactly_one_of (list of groups; both-set and none-set are errors). Fields in a
# group are also listed in `optional` so code that walks "every field this type
# accepts" (required + optional) still sees them.
#
# The presence contract (required + exactly_one_of) is shared with
# docs/schemas/tenant-config.schema.json, the hub:
# tests/shared/test_receiver_spec_parity.py pins this dict to it (including the
# accepted field set). No Go copy judges receivers (ADR-036 §9).
# Optional values (#2295) are read from the schema at run time
# (_lib_validation.receiver_optional_problem): a property referencing
# definitions.yamlBool takes a boolean or null (#2906: the schema's YAML 1.1
# boolean words are refused as strings — the generator judges by type), and
# http_config follows Alertmanager's rules (HTTP_CONFIG_AUTH_FIELDS below).
RECEIVER_TYPES: Final[dict[str, dict[str, Any]]] = {
    "webhook": {
        "am_key": "webhook_configs",
        "required": ["url"],
        "optional": ["send_resolved", "http_config"],
    },
    "email": {
        "am_key": "email_configs",
        # `from` is required per-receiver: the platform configures SMTP per
        # receiver (smarthost) with no global smtp_* block, and Alertmanager
        # rejects an email_config without `from` UNLESS a global smtp_from is set
        # ("no global SMTP from set" at config load). Same per-receiver SMTP
        # assumption that already makes `smarthost` required.
        "required": ["to", "smarthost", "from"],
        "optional": ["auth_username", "auth_password", "require_tls",
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
        "required": [],
        # Alertmanager needs one of the two, and given both it uses the Events
        # API v1 via service_key and silently ignores routing_key
        # (notify/pagerduty/pagerduty.go) — so both-set is rejected as well.
        "exactly_one_of": [["service_key", "routing_key"]],
        "optional": ["service_key", "routing_key", "severity", "description",
                      "client", "client_url", "send_resolved"],
    },
}

# http_config keys Alertmanager accepts at most ONE of (#2295):
# prometheus/common HTTPClientConfig.Validate refuses every pair, and the whole
# config then fails to reload. basic_auth / oauth2 / authorization are mappings
# (an empty mapping counts as set); bearer_token is a string ("" counts as
# unset). Rows of receiver_presence_cases.json pin it. Alertmanager's fifth,
# bearer_token_file, is not here: #3257 refuses every file-reference key of a
# conf.d receiver before this rule is read
# (_lib_validation.receiver_file_reference_problem).
HTTP_CONFIG_AUTH_FIELDS: Final[tuple[str, ...]] = (
    "basic_auth", "oauth2", "authorization", "bearer_token",
)
HTTP_CONFIG_AUTH_MAPPINGS: Final[frozenset[str]] = frozenset(
    {"basic_auth", "oauth2", "authorization"})

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
    "pagerduty":  [],                 # service_key / routing_key, no URL
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
