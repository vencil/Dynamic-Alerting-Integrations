"""Validation and parsing helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional, Union

from _lib_compat import find_project_root
from _lib_constants import (
    _DISABLED_VALUES,
    _DURATION_MULTIPLIERS,
    _DURATION_RE,
    GUARDRAILS,
    HTTP_CONFIG_AUTH_FIELDS,
    HTTP_CONFIG_AUTH_MAPPINGS,
    PLATFORM_DEFAULTS,
    RECEIVER_TYPES,
)


def detect_cli_lang() -> str:
    """Detect CLI language from environment variables.

    Checks in order: ``DA_LANG``, ``LC_ALL``, ``LANG``.
    Returns ``'zh'`` if any starts with ``zh``, ``'en'`` otherwise.
    """
    for var in ("DA_LANG", "LC_ALL", "LANG"):
        val: str = os.environ.get(var, "")
        if val.startswith("zh"):
            return "zh"
        if val.startswith("en"):
            return "en"
    return "en"


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


def positive_seconds(value: str) -> float:
    """argparse ``type=`` for a timeout or interval in seconds (#2942).

    A finite number above 0 and at most ``threading.TIMEOUT_MAX`` (about 292
    years), fractions allowed; anything else is refused, so argparse exits 2
    (CALLER_ERROR) before any work. A ``urllib`` timeout of 0 makes the socket
    non-blocking (every call fails at once), a negative one raises ValueError,
    and one past TIMEOUT_MAX raises OverflowError in ``socket.settimeout`` /
    ``time.sleep``: none is a usable value.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}") from None
    if not math.isfinite(seconds) or seconds <= 0 or seconds > threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a number of seconds above 0 and at most "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return seconds


def non_negative_seconds(value: str) -> float:
    """argparse ``type=`` for a pause in seconds where 0 means "do not wait" (#2966).

    The sibling of :func:`positive_seconds` for a rate limit, a throttle or a
    poll interval: a finite number from 0 up to ``threading.TIMEOUT_MAX``,
    fractions allowed; anything else is refused, so argparse exits 2
    (CALLER_ERROR) before any work. A negative pause used to be skipped
    silently by a ``> 0`` guard (or raise ValueError in ``time.sleep``), and
    ``inf`` raised OverflowError in ``time.sleep`` partway through a run.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not a number of seconds: {value!r}") from None
    if not math.isfinite(seconds) or seconds < 0 or seconds > threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a number of seconds from 0 to "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return seconds


def positive_int(value: str) -> int:
    """argparse ``type=`` for a count that must be at least 1 (#2966).

    ``int()`` parsing (so ``"1.5"`` is refused), then ``>= 1``; anything else
    raises ArgumentTypeError, so argparse exits 2 (CALLER_ERROR) with a
    message that names the problem instead of the function.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def positive_int_seconds(value: str) -> int:
    """argparse ``type=`` for a whole number of seconds above 0 (#2966).

    For a value that is both slept on and used in integer arithmetic
    (``duration // interval`` feeding ``range()``), where a float would turn a
    sample count into ``40.0`` and break ``range``. ``int()`` parsing, then 1
    up to the ``threading.TIMEOUT_MAX`` ceiling of :func:`positive_seconds`
    (past it ``time.sleep`` raises OverflowError). ⚠️ Not a ceiling for a
    ``subprocess`` timeout: that waits in poll(), which overflows past
    2147483 s on Linux.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= number <= threading.TIMEOUT_MAX:
        raise argparse.ArgumentTypeError(
            f"must be a whole number of seconds from 1 to "
            f"{threading.TIMEOUT_MAX:.0f}: {value!r}")
    return number


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


def is_disabled(value: Any) -> bool:
    """Check if *value* represents a disabled state.

    Recognises: ``disable``, ``disabled``, ``off``, ``false``
    (case-insensitive, stripped). Consistent with the Go
    ``IsDisabledValue()`` implementation.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _DISABLED_VALUES


# The severities a threshold value's `:severity` suffix may name: Go's
# config.ThresholdSeverities (#3071), the ones the rule packs' threshold
# recording rules select. tests/shared/threshold_value_matrix.json pins it.
THRESHOLD_SEVERITIES = ("warning", "critical")

# What Go's strings.TrimSpace trims (unicode.IsSpace). Python's str.strip also
# trims U+001C..U+001F, which Go keeps.
GO_SPACE = ("\t\n\v\f\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005"
            "\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000")


def split_threshold_severity(raw: Any) -> tuple[str, str]:
    """`(value, severity)` of a threshold value written `value[:severity]`,
    as the exporter's splitThresholdSeverity reads it (#3071): split at the
    LAST `:`, trim both halves as Go does, lower-case an ASCII severity; no
    `:` is `warning`. A severity outside THRESHOLD_SEVERITIES (`70:high`, the
    empty `70:`) raises ValueError: the exporter cannot read the value
    either. The value half is returned as written (trimmed); whether it is a
    number is the caller's question. The Python readers of the grammar
    (backtest_threshold, check_threshold_unit_sanity) call this one;
    custom_alerts/shape.py keeps its own stdlib-only copy. All are held to
    Go by tests/shared/test_threshold_value_parity.py."""
    text = str(raw).strip(GO_SPACE)
    value, sep, suffix = text.rpartition(":")
    if not sep:
        return text, "warning"
    suffix = suffix.strip(GO_SPACE)
    severity = suffix.lower() if suffix.isascii() else suffix
    if severity not in THRESHOLD_SEVERITIES:
        raise ValueError(f"threshold severity {suffix!r} must be warning or critical")
    return value.strip(GO_SPACE), severity


def custom_alert_disabled(threshold: Any) -> bool:
    """Whether a custom alert with this `threshold` is the three-state
    opt-out, exactly as resolveOneCustomAlert decides it (#3071): the value
    half of `split_threshold_severity` is a disabled word (`disable`,
    `disable:critical`, `OFF`). A suffix the grammar refuses
    (`disable:bogus`, `disable:`) is a parse error in Go, not an opt-out.
    Lower-cased the way Go's strings.ToLower does (per character; U+0130 →
    `i`, which Python's full mapping turns into two characters). Pinned
    against Go by tests/shared/threshold_value_matrix.json's
    `custom_alert_disabled` column."""
    try:
        value, _severity = split_threshold_severity(threshold)
    except ValueError:
        return False
    return "".join("i" if c == "\u0130" else c.lower() for c in value) in _DISABLED_VALUES


def is_valid_tenant_id(tenant: Any) -> bool:
    """Whether *tenant* is a valid tenant id (ADR-035; #2341 R8).

    The rule is ``tenant_id_rule()`` — tenant-config.schema.json
    ``definitions.tenantId``, a DNS-1123 label — matched with ``fullmatch``.
    Go twin: pkg/tenantid.Valid. Pinned by the
    ``tenant_ids`` table of tests/shared/routing_policy_parity_matrix.json.
    Raises RuntimeError when the schema cannot be read (fail-closed).
    """
    return isinstance(tenant, str) and tenant_id_rule()[0].fullmatch(tenant) is not None


def validate_and_clamp(
    param: str,
    value: Union[str, int, float],
    tenant: str,
) -> tuple[Union[str, int, float], list[str]]:
    """Validate a timing parameter against guardrails and clamp if needed.

    #2490: *value* is read as Alertmanager reads it (``am_duration_seconds``):
    ``1h30m`` and ``1d`` are durations, ``1.5h`` / ``30m1h`` / ``1ns`` are
    not. A clamped value is written by ``format_duration`` (h / m / s only),
    which Alertmanager reads; a value within bounds is returned unchanged.

    Args:
        param: Parameter name (``group_wait``, ``group_interval``,
               ``repeat_interval``).
        value: Duration string (e.g. ``"30s"``), or numeric seconds (kept
               for direct callers; the route generator always passes the
               YAML value's ``str()``, where a bare number has no unit).
        tenant: Tenant identifier (used in warning messages).

    Returns:
        A 2-tuple ``(clamped_value, warnings)`` where *warnings* is a
        list of human-readable strings (empty if within bounds).
    """
    warnings: list[str] = []

    if param not in GUARDRAILS:
        return value, warnings

    min_sec, max_sec, desc = GUARDRAILS[param]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds: Optional[float] = float(value)
    else:
        seconds = am_duration_seconds(value if isinstance(value, str) else str(value))

    if seconds is None:
        warnings.append(f"  WARN: {tenant}: invalid {param} '{value}' "
                        f"({INVALID_DURATION_HINT}), using platform default "
                        f"{PLATFORM_DEFAULTS.get(param, value)}")
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


FIELD_UNSET = "unset"
FIELD_SET = "set"
FIELD_NOT_STRING = "not_string"


def receiver_field_state(receiver: dict[str, Any], field: str) -> str:
    """State of an exactly-one group field, by the schema's type rule (#2137).

    Cases:
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json
    ``FIELD_NOT_STRING`` is an error,
    stricter than Alertmanager on purpose: it renders ``0`` as ``"0"`` but
    fails to load ``[]`` (``cannot unmarshal !!seq``), so no "counts as given"
    rule for non-strings is right for all of them.
    """
    value = receiver.get(field)
    if value is None or value == "":
        return FIELD_UNSET
    if isinstance(value, str):
        return FIELD_SET
    return FIELD_NOT_STRING


def receiver_exactly_one_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """Check the ``exactly_one_of`` groups of ``RECEIVER_TYPES[rtype]``.

    Each field's state comes from ``receiver_field_state``; a non-string value
    is reported first, and only when every field's type is valid is "exactly
    one" checked. Returns the first problem (callers prefix tenant / receiver
    context), or ``None`` when every group has exactly one field set. Unknown
    types return ``None``; callers reject those separately.
    """
    spec = RECEIVER_TYPES.get(rtype, {})
    for group in spec.get("exactly_one_of", []):
        states = {f: receiver_field_state(receiver, f) for f in group}
        for f, state in states.items():
            if state == FIELD_NOT_STRING:
                return (f"field '{f}' must be a string, "
                        f"got {type(receiver[f]).__name__}")
        set_fields = [f for f, state in states.items() if state == FIELD_SET]
        if len(set_fields) == 1:
            continue
        names = " or ".join(f"'{f}'" for f in group)
        if not set_fields:
            return f"requires exactly one of {names}, none is set"
        problem = f"requires exactly one of {names}, not both"
        if rtype == "pagerduty":
            problem += (" (Alertmanager would use the Events API v1 via "
                        "service_key and silently ignore routing_key)")
        return problem
    return None


_TENANT_SCHEMA_BASENAME = "tenant-config.schema.json"
_RECEIVER_FIELD_SCHEMAS: Optional[dict[str, dict[str, dict[str, Any]]]] = None


def _find_tenant_schema() -> Optional[Path]:
    """Locate tenant-config.schema.json in either shipping layout.

    Flat first: the da-tools image copies it beside this module (build.sh
    ``REPO_DATA_FILES``, paired by ``REQUIRED_DATA_FILES`` in
    check_build_completeness.py) and has no repo tree. Otherwise
    ``docs/schemas/`` under THIS tree's root (``find_project_root``; #1494 no
    counting of levels, #2845 never an enclosing project's).
    """
    here = Path(__file__).resolve().parent
    flat = here / _TENANT_SCHEMA_BASENAME
    if flat.is_file():
        return flat
    repo_root = find_project_root(here)
    if repo_root is not None:
        candidate = repo_root / "docs" / "schemas" / _TENANT_SCHEMA_BASENAME
        if candidate.is_file():
            return candidate
    return None


def _receiver_field_schemas() -> dict[str, dict[str, dict[str, Any]]]:
    """``{rtype: {field: {"type": ..., "pattern": ...}}}`` read from the schema.

    The schema is the hub of the receiver field contract (#2180): the
    ``pattern`` of a URL / smarthost field is written once there (a
    ``definitions`` entry the property references via ``allOf``) and read
    here, never copied. Fails closed — a missing schema raises instead of
    silently skipping the format check.
    """
    global _RECEIVER_FIELD_SCHEMAS
    if _RECEIVER_FIELD_SCHEMAS is not None:
        return _RECEIVER_FIELD_SCHEMAS
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; receiver fields cannot be checked")
    with open(path, encoding="utf-8") as f:
        defs = json.load(f)["definitions"]

    def resolve(prop: dict[str, Any]) -> dict[str, Any]:
        out = {"type": prop.get("type"), "pattern": prop.get("pattern"),
               "ref": prop.get("$ref")}
        for sub in prop.get("allOf", []):
            ref = sub["$ref"]
            if not ref.startswith("#/definitions/"):
                raise RuntimeError(f"unsupported $ref {ref!r} in {path}")
            target = defs[ref[len("#/definitions/"):]]
            if target.get("pattern"):
                out["pattern"] = target["pattern"]
        return out

    table: dict[str, dict[str, dict[str, Any]]] = {}
    for branch in defs["receiver"]["oneOf"]:
        d = defs[branch["$ref"][len("#/definitions/"):]]
        table[d["properties"]["type"]["const"]] = {
            f: resolve(p) for f, p in d["properties"].items() if f != "type"}
    _RECEIVER_FIELD_SCHEMAS = table
    return table


_TENANT_ID_RULE: Optional[tuple[re.Pattern[str], str]] = None


def tenant_id_rule() -> tuple[re.Pattern[str], str]:
    """``(compiled pattern, description)`` of the tenant-id rule (ADR-035).

    Read from tenant-config.schema.json ``definitions.tenantId``, the one
    authored copy; Go and the portal read copies generated from it by
    scripts/tools/dx/gen_tenant_id_json.py. Match with ``fullmatch``: Python's
    ``$`` also matches before a trailing newline. Fails closed like
    ``_receiver_field_schemas`` (#2180): a missing schema or definition raises
    instead of letting every id through.
    """
    global _TENANT_ID_RULE
    if _TENANT_ID_RULE is not None:
        return _TENANT_ID_RULE
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; tenant ids cannot be checked")
    with open(path, encoding="utf-8") as f:
        definition = json.load(f).get("definitions", {}).get("tenantId")
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("pattern"), str)
            or not isinstance(definition.get("description"), str)
            or not definition["pattern"] or not definition["description"]):
        raise RuntimeError(
            f"{path}: definitions.tenantId with a string pattern and description "
            "is missing; tenant ids cannot be checked")
    _TENANT_ID_RULE = (re.compile(definition["pattern"]), definition["description"])
    return _TENANT_ID_RULE


_DURATION_RULE: Optional[tuple[re.Pattern[str], int, str]] = None

# Operator-facing summary of definitions.duration, for invalid-value lines.
INVALID_DURATION_HINT = ("not an Alertmanager duration: whole numbers with units "
                         "largest first, e.g. 30s, 5m, 1h30m, 1d")

# prometheus/common model.ParseDuration's units (time.go unitMap), in the
# order a value must give them. Nanoseconds per unit.
_AM_DURATION_UNIT_NS: dict[str, int] = {
    "y": 365 * 86400 * 10**9, "w": 7 * 86400 * 10**9, "d": 86400 * 10**9,
    "h": 3600 * 10**9, "m": 60 * 10**9, "s": 10**9, "ms": 10**6,
}
_AM_DURATION_TOKEN_RE = re.compile(r"([0-9]+)(ms|y|w|d|h|m|s)")
_INT64_MAX = (1 << 63) - 1


def duration_rule() -> tuple[re.Pattern[str], int, str]:
    """``(compiled pattern, minLength, description)`` of the routing timing
    syntax (#2490) — tenant-config.schema.json ``definitions.duration``, the
    one authored copy, which states Alertmanager's ``model.ParseDuration``.

    Match with ``fullmatch`` (Python's ``$`` also matches before a trailing
    newline). Fails closed like ``tenant_id_rule``: a missing schema or
    definition raises instead of letting every value through.
    """
    global _DURATION_RULE
    if _DURATION_RULE is not None:
        return _DURATION_RULE
    path = _find_tenant_schema()
    if path is None:
        raise RuntimeError(
            f"{_TENANT_SCHEMA_BASENAME} not found beside {__file__} or under "
            "docs/schemas/ of the project root; durations cannot be checked")
    with open(path, encoding="utf-8") as f:
        definition = json.load(f).get("definitions", {}).get("duration")
    if (not isinstance(definition, dict)
            or not isinstance(definition.get("pattern"), str)
            or not definition["pattern"]
            or not isinstance(definition.get("minLength"), int)
            or not isinstance(definition.get("description"), str)):
        raise RuntimeError(
            f"{path}: definitions.duration with a string pattern, an integer "
            "minLength and a description is missing; durations cannot be checked")
    _DURATION_RULE = (re.compile(definition["pattern"]), definition["minLength"],
                      definition["description"])
    return _DURATION_RULE


def am_duration_seconds(value: Any) -> Optional[float]:
    """Seconds Alertmanager reads from a routing timing *value*, or ``None``
    when Alertmanager refuses it (#2490).

    The syntax is ``duration_rule()`` (the schema's ``definitions.duration``);
    on top of it, the one check a pattern cannot state — prometheus/common
    ``ParseDuration``'s int64-nanosecond overflow (``"duration out of
    range"``), computed the way it does: per unit ``v > 2**63 // unit``, then
    the running sum ``> 2**63 - 1``. Only a ``str`` is a duration: Alertmanager
    reads the YAML text, and an unquoted ``30`` has no unit.

    ⛔ Not ``parse_duration_seconds``: that one serves the CLI arguments of
    other tools (``7d``, ``1.5h``, bare numbers) and stays as it is.
    tests/shared/am_duration_matrix.json (written by Go) pins this function.
    """
    if not isinstance(value, str):
        return None
    pattern, min_len, _desc = duration_rule()
    if len(value) < min_len or pattern.fullmatch(value) is None:
        return None
    if value == "0":
        return 0.0
    total = 0
    for digits, unit in _AM_DURATION_TOKEN_RE.findall(value):
        v, mult = int(digits), _AM_DURATION_UNIT_NS[unit]
        if v > (1 << 63) // mult:
            return None
        total += v * mult
        if total > _INT64_MAX:
            return None
    return total / 1e9


_YAML_BOOL_REF = "#/definitions/yamlBool"
_HTTP_CONFIG_REF = "#/definitions/httpConfigOrNull"
#: The YAML 1.1 boolean words PyYAML's resolver reads as booleans
#: (true / false / yes / no / on / off, three spellings each).
_YAML_BOOL_LITERALS: dict[str, bool] = {
    spelling: word in ("true", "yes", "on")
    for word in ("true", "false", "yes", "no", "on", "off")
    for spelling in (word, word.capitalize(), word.upper())}


def yaml_bool_literals() -> dict[str, bool]:
    """``{literal: value}`` of the YAML 1.1 boolean words (#2295): the
    plain scalars PyYAML reads as booleans. ⛔ #2906: no reader of the
    routing plane takes any of them as a STRING — the route generator reads
    yaml.v3 (an unquoted ``yes`` is the string "yes") and judges a boolean
    field by type alone (ADR-036 §2, ``is_bool_or_unset``), and the schema's
    ``definitions.yamlBool`` is boolean or null only, so this is no longer a
    schema list. What still uses the table: the YAML 1.1 boolean-word
    warning — ``_lib_io.find_yaml11_bool_words`` takes its words from
    ``_YAML_BOOL_LITERALS`` minus yaml.v3's six (``true`` / ``false`` in
    three spellings) — and that warning's tests, through this function
    (tests/shared/test_yaml11_bool_word_warning.py). ``y`` / ``n`` are not in
    it: PyYAML reads them as strings."""
    return dict(_YAML_BOOL_LITERALS)


def is_bool_or_unset(value: Any) -> bool:
    """#2906: what a boolean field of the route generator accepts — a
    boolean, or null (unset). Judged by type alone (ADR-036 §2): a string is
    refused whatever it says, ``"true"`` / ``"yes"`` included. An unquoted
    ``yes`` is a boolean only because PyYAML (YAML 1.1) built one."""
    return value is None or isinstance(value, bool)


def receiver_bool_fields(rtype: str) -> list[str]:
    """The optional fields of ``rtype`` the schema types as a YAML boolean."""
    return [f for f, prop in _receiver_field_schemas().get(rtype, {}).items()
            if prop.get("ref") == _YAML_BOOL_REF]


def receiver_bool_problems(rtype: str, receiver: Any) -> list[tuple[str, Any]]:
    """#2906: ``(field, value)`` of each boolean field of a *rtype* receiver
    that is not ``is_bool_or_unset`` — the schema's ``yamlBool`` properties
    (``send_resolved``, email ``require_tls``) and
    ``http_config.proxy_from_environment``. *field* is relative to the
    receiver (``http_config.proxy_from_environment``). An unknown type has no
    boolean field here (``unknown_receiver_type`` names it)."""
    if not isinstance(receiver, dict):
        return []
    out = [(f, receiver[f]) for f in receiver_bool_fields(rtype)
           if f in receiver and not is_bool_or_unset(receiver[f])]
    for field, prop in _receiver_field_schemas().get(rtype, {}).items():
        if prop.get("ref") != _HTTP_CONFIG_REF:
            continue
        http_config = receiver.get(field)
        if (isinstance(http_config, dict) and "proxy_from_environment" in http_config
                and not is_bool_or_unset(http_config["proxy_from_environment"])):
            out.append((f"{field}.proxy_from_environment",
                        http_config["proxy_from_environment"]))
    return out


def _type_name(value: Any) -> str:
    """YAML-ish name of a decoded value's type, for messages."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return f"string {value!r}"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


def _http_config_problem(value: Any) -> Optional[str]:
    """First problem of an ``http_config`` value, or ``None`` (#2295).

    Alertmanager's own rule (prometheus/common HTTPClientConfig / ProxyConfig
    as amtool 0.34.1 loads them), stricter only where the shared case table
    says ``strict``:

    - null is unset; otherwise a mapping;
    - ``HTTP_CONFIG_AUTH_MAPPINGS`` keys: null is unset, else a mapping (set
      even when empty); the other ``HTTP_CONFIG_AUTH_FIELDS``: null and ``""``
      are unset, else a string (a number or date is refused: PyYAML has
      already rewritten its text, '0123' → 83); at most one set;
    - ``proxy_url`` / ``no_proxy``: null and ``""`` are unset; else a string
      (strict for the same reason). Whether a proxy_url
      parses as a URL is NOT checked here: that is Go's net/url.Parse, and the
      generator's ``--validate`` hands the rendered config to amtool, which
      runs it;
    - ``proxy_from_environment`` is a boolean or null (#2906: never a
      string, whatever it says — ``is_bool_or_unset``); true together with a
      non-empty proxy_url or a no_proxy is refused; no_proxy needs a
      proxy_url key; proxy_connect_header needs a non-empty proxy_url or
      proxy_from_environment.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return f"field 'http_config' must be a mapping, got {_type_name(value)}"
    set_keys = []
    for key in HTTP_CONFIG_AUTH_FIELDS:
        item = value.get(key)
        if item is None:
            continue
        if key in HTTP_CONFIG_AUTH_MAPPINGS:
            if not isinstance(item, dict):
                return (f"field 'http_config.{key}' must be a mapping, "
                        f"got {_type_name(item)}")
            set_keys.append(key)
        elif not isinstance(item, str):
            return (f"field 'http_config.{key}' must be a string, "
                    f"got {_type_name(item)}")
        elif item != "":
            set_keys.append(key)
    if len(set_keys) > 1:
        return (f"http_config sets {', '.join(set_keys)}; Alertmanager accepts at "
                f"most one of {', '.join(HTTP_CONFIG_AUTH_FIELDS)}")
    proxy = value.get("proxy_url")
    proxy_key = proxy is not None
    if proxy is not None and not isinstance(proxy, str):
        return f"field 'http_config.proxy_url' must be a string, got {_type_name(proxy)}"
    proxy_text = proxy is not None and proxy != ""
    from_env = False
    if "proxy_from_environment" in value:
        pfe = value["proxy_from_environment"]
        if not is_bool_or_unset(pfe):
            return (f"field 'http_config.proxy_from_environment' must be true or "
                    f"false, got {_type_name(pfe)}")
        from_env = pfe is True
    no_proxy_value = value.get("no_proxy")
    if no_proxy_value is not None and not isinstance(no_proxy_value, str):
        return f"field 'http_config.no_proxy' must be a string, got {_type_name(no_proxy_value)}"
    no_proxy = no_proxy_value is not None and no_proxy_value != ""
    header = value.get("proxy_connect_header")
    if header is not None and not isinstance(header, dict):
        return (f"field 'http_config.proxy_connect_header' must be a mapping, "
                f"got {_type_name(header)}")
    if header and not from_env and not proxy_text:
        return "http_config: proxy_connect_header needs a non-empty proxy_url or proxy_from_environment: true"
    if from_env and proxy_text:
        return "http_config: proxy_url must not be set together with proxy_from_environment: true"
    if from_env and no_proxy:
        return "http_config: no_proxy must not be set together with proxy_from_environment: true"
    if no_proxy and not proxy_key:
        return "http_config: no_proxy needs a proxy_url"
    return None


# #3257: the receiver keys that make Alertmanager load something other than
# the value written — a file inside its own container (`*_file`, and the
# `files` list of an `http_headers` entry), or a secret through a secret
# manager Alertmanager does not have (`*_ref`: the config loads, then building
# the receiver fails and the whole reload with it). A receiver written in the
# tenant configs (tenant, defaults, overrides, enforced, profiles) must not
# carry any of them, at any depth. A RULE, not a list of today's keys
# (cf. Grafana Mimir's validateFileLoad, which refuses `_file` the same way):
# a key Alertmanager adds upstream is covered without a change here. Pinned
# by tests/shared/test_receiver_file_reference_rule.py (the schema has no
# property the rule matches anywhere a receiver reaches) and by
# tests/alertmanager-inhibit (the schema's http_config key sets are
# Alertmanager's minus exactly these keys). A secret the platform itself
# needs belongs in the base config (--base-config), which this rule never
# reads.
FILE_REFERENCE_KEY_SUFFIXES: tuple[str, ...] = ("_file", "_ref")
FILE_REFERENCE_KEYS: frozenset[str] = frozenset({"files"})


def is_file_reference_key(key: Any) -> bool:
    """#3257: whether *key* is a receiver key the rule above refuses."""
    return isinstance(key, str) and (
        key.endswith(FILE_REFERENCE_KEY_SUFFIXES) or key in FILE_REFERENCE_KEYS)


def file_reference_paths(value: Any, path: str = "") -> list[str]:
    """#3257: the path of every key of *value* (a receiver, walked through
    every nested mapping and list) that ``is_file_reference_key``: dotted,
    list items as ``[i]`` — ``http_config.basic_auth.password_file``,
    ``http_config.http_headers.X-Token.files``. The value under such a key
    does not matter (null and ``""`` included): the key itself is refused,
    as the schema refuses it (no such property, additionalProperties: false).
    A key that is not a string (a ``!!binary`` key reads as bytes, and
    Alertmanager decodes ``!!binary password_file`` to ``password_file``) is
    refused too: the rule cannot read its name, so it does not lean on the
    file reader having refused the tag first."""
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = key if isinstance(key, str) else repr(key)
            here = f"{path}.{name}" if path else name
            if not isinstance(key, str) or is_file_reference_key(key):
                out.append(here)
            else:
                out.extend(file_reference_paths(item, here))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            out.extend(file_reference_paths(item, f"{path}[{i}]"))
    return out


def file_reference_problem(paths: list[str]) -> Optional[str]:
    """#3257: the message for the ``file_reference_paths`` of one receiver,
    or ``None`` when there are none."""
    if not paths:
        return None
    names = ", ".join(f"'{p}'" for p in paths)
    return (f"sets {names}: a key ending in _file or _ref, or a 'files' list, "
            "makes Alertmanager load a file or secret from inside its own "
            "container, so a tenant-config receiver may not carry one; write the value "
            "itself, or have the platform set it in the base config")


def receiver_file_reference_problem(receiver: Any) -> Optional[str]:
    """#3257: the one judgment of the rule above for a receiver — the
    problem (callers prefix tenant / receiver context), or ``None``."""
    return file_reference_problem(file_reference_paths(receiver))


def receiver_optional_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """Check the optional values Alertmanager cannot load (#2295).

    Read from tenant-config.schema.json like the required-field formats:

    - every property of the type referencing ``yamlBool`` (``send_resolved``,
      email ``require_tls``) must be a boolean or null (unset) —
      ``send_resolved: maybe`` makes Alertmanager refuse the whole config,
      and since #2906 a string is refused whatever it says (``"yes"``,
      ``"true"``): the generator judges by type (ADR-036 §2). The route
      generator refuses the whole tree on these before any receiver is
      built (``receiver_bool_problems``); this check stays for the callers
      that build a receiver directly;
    - the property referencing ``httpConfigOrNull`` (webhook ``http_config``)
      follows ``_http_config_problem``;
    - before both, #3257: no key at any depth may be a file reference
      (``receiver_file_reference_problem``).

    Returns the first problem (callers prefix tenant / receiver context), or
    ``None``. Cases in
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json.
    """
    # #3257: first — a file-reference key is never written out, whatever
    # else the receiver gets wrong. The route generator refuses the whole
    # tree on it before any receiver is built
    # (`_grar_validate.receiver_file_reference_errors`); this check stays for
    # the callers that build a receiver directly.
    problem = receiver_file_reference_problem(receiver)
    if problem:
        return problem
    fields = _receiver_field_schemas().get(rtype, {})
    for field in receiver_bool_fields(rtype):
        if field in receiver and not is_bool_or_unset(receiver[field]):
            return (f"field '{field}' must be true or false, "
                    f"got {_type_name(receiver[field])}")
    for field, prop in fields.items():
        if prop.get("ref") == _HTTP_CONFIG_REF and field in receiver:
            problem = _http_config_problem(receiver[field])
            if problem:
                return problem
    return None


def receiver_required_problem(rtype: str, receiver: dict[str, Any], field: str) -> Optional[str]:
    """Check one ``required`` field of ``RECEIVER_TYPES[rtype]`` (#2180).

    Presence is ``receiver_field_state`` (unset = missing, null or ``""``);
    everything else follows the field's type in tenant-config.schema.json:

    - a string field must be a string, and match the schema's ``pattern``
      when it has one (``re.fullmatch``: Python's ``$`` in ``re.search``
      also matches before a final newline, which ECMA and Go RE2 do not);
    - an array field (email ``to``) given as a list needs at least one
      item, each a non-empty string — the list is joined into
      Alertmanager's ``to`` string, where ``[""]`` reads as no address.
      A plain string is still taken for it: Alertmanager's own ``to`` is a
      string (the schema alone rejects that form).

    Returns the problem (callers prefix tenant / receiver context), or
    ``None``. Cases in
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json.
    """
    state = receiver_field_state(receiver, field)
    if state == FIELD_UNSET:
        return f"requires '{field}'"
    value = receiver[field]
    prop = _receiver_field_schemas().get(rtype, {}).get(field, {})
    if state == FIELD_NOT_STRING:
        if not (prop.get("type") == "array" and isinstance(value, list)):
            return f"field '{field}' must be a string, got {type(value).__name__}"
        if not value:
            return f"requires '{field}'"
        for i, item in enumerate(value):
            if not isinstance(item, str) or item == "":
                return f"field '{field}' item {i} must be a non-empty string, got {item!r}"
        return None
    pattern = prop.get("pattern")
    if pattern and re.fullmatch(pattern, value) is None:
        return (f"field '{field}' value {value!r} is not in the format "
                "tenant-config.schema.json requires")
    return None


def i18n_text(zh: str, en: str) -> str:
    """Return *zh* or *en* based on ``detect_cli_lang()`` result.

    Intended as a lightweight ``window.__t`` equivalent for Python CLI tools,
    replacing ad-hoc ``msg = zh if _LANG == 'zh' else en`` patterns.
    """
    return zh if detect_cli_lang() == "zh" else en
