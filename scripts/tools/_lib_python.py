"""Shared library for Dynamic Alerting Python tools.

v2.3.0: Split into sub-modules for reduced coupling.
This file serves as a backward-compatible facade.
Direct imports from sub-modules are preferred for new code:
  from _lib_constants import GUARDRAILS
  from _lib_io import write_text_secure
  from _lib_validation import parse_duration_seconds
  from _lib_prometheus import query_prometheus_instant
"""
from __future__ import annotations

# Re-export all public symbols for backward compatibility
from _lib_constants import (  # noqa: F401
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
    GUARDRAILS,
    PLATFORM_DEFAULTS,
    RECEIVER_TYPES,
    RECEIVER_URL_FIELDS,
    JOB_DB_MAP,
    METRIC_PREFIX_DB_MAP,
    ONBOARD_HINTS_FILENAME,
    _ALLOWED_SCHEMES,
)

from _lib_io import (  # noqa: F401
    load_yaml_file,
    iter_yaml_files,
    load_tenant_configs,
    write_text_secure,
    write_json_secure,
    write_onboard_hints,
    read_onboard_hints,
    format_json_report,
)

from _lib_validation import (  # noqa: F401
    parse_duration_seconds,
    format_duration,
    is_disabled,
    validate_and_clamp,
    detect_cli_lang,
    i18n_text,
)

from _lib_prometheus import (  # noqa: F401
    _validate_url_scheme,
    http_get_json,
    http_post_json,
    http_request_with_retry,
    query_prometheus_instant,
)
