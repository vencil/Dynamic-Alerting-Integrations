"""Validation and parsing helpers for Dynamic Alerting platform.

Split from _lib_python.py in v2.3.0 for reduced coupling.
Import via _lib_python.py facade for backward compatibility.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional, Union

from _lib_compat import PROJECT_ROOT_MARKERS
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


def is_valid_tenant_id(tenant: Any) -> bool:
    """Whether *tenant* is a valid tenant id (ADR-035; #2341 R8).

    The rule is ``tenant_id_rule()`` — tenant-config.schema.json
    ``definitions.tenantId``, a DNS-1123 label — matched with ``fullmatch``.
    Go twin: routingpolicy.IsValidTenantID (pkg/tenantid). Pinned by the
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

    Same rule as the Go copy (pkg/receiverspec exactlyOneProblem).
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
    check_build_completeness.py) and carries no project-root marker. The repo
    branch walks up to a marker, bounded there, then reads
    ``docs/schemas/``. Same shape as ``_grar_validate._find_platform_rules_configmap``
    (#1494): no counting of directory levels.
    """
    here = Path(__file__).resolve().parent
    flat = here / _TENANT_SCHEMA_BASENAME
    if flat.is_file():
        return flat
    repo_root = next(
        (base for base in (here, *here.parents)
         if any((base / m).exists() for m in PROJECT_ROOT_MARKERS)),
        None,
    )
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
    global _YAML_BOOL_LITERALS
    _YAML_BOOL_LITERALS = {
        lit: lit.lower() in ("true", "yes", "on")
        for lit in defs["yamlBool"]["anyOf"][1]["enum"]}
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


_YAML_BOOL_REF = "#/definitions/yamlBool"
_HTTP_CONFIG_REF = "#/definitions/httpConfigOrNull"
_YAML_BOOL_LITERALS: dict[str, bool] = {}


def yaml_bool_literals() -> dict[str, bool]:
    """``{literal: value}`` of the YAML 1.1 boolean words (#2295).

    Read from tenant-config.schema.json ``definitions.yamlBool`` (the one
    authored list; the Go copy is pkg/receiverspec YAML11BoolLiterals). PyYAML
    already reads these plain words as booleans; the strings reach here only
    when quoted, or from JSON, and Go's yaml.v3 reads yes / no / on / off as
    strings, so both sides take them and the generator writes a boolean.
    ``y`` / ``n`` are not in it: a documented deliberate refusal.
    """
    _receiver_field_schemas()
    return _YAML_BOOL_LITERALS


def _is_yaml_bool(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return True
    return isinstance(value, str) and value in yaml_bool_literals()


def _yaml_bool_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and yaml_bool_literals().get(value, False)


def receiver_bool_fields(rtype: str) -> list[str]:
    """The optional fields of ``rtype`` the schema types as a YAML boolean."""
    return [f for f, prop in _receiver_field_schemas().get(rtype, {}).items()
            if prop.get("ref") == _YAML_BOOL_REF]


def coerce_yaml_bool(value: Any) -> Any:
    """A YAML 1.1 boolean word as the boolean it means; anything else as is."""
    if isinstance(value, str) and value in yaml_bool_literals():
        return yaml_bool_literals()[value]
    return value


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
    says ``strict``; the Go copy is pkg/receiverspec checkHTTPConfig:

    - null is unset; otherwise a mapping;
    - ``HTTP_CONFIG_AUTH_MAPPINGS`` keys: null is unset, else a mapping (set
      even when empty); the other ``HTTP_CONFIG_AUTH_FIELDS``: null and ``""``
      are unset, else a string (a number or date is refused: PyYAML has
      already rewritten its text, '0123' → 83); at most one set;
    - ``proxy_url`` / ``no_proxy``: null and ``""`` are unset; else a string
      (strict for the same reason). Whether a proxy_url
      parses as a URL is NOT checked here: that is Go's net/url.Parse, and the
      generator's ``--validate`` hands the rendered config to amtool, which
      runs it (the Go copy checks it itself, ProxyURLProblem);
    - ``proxy_from_environment`` is a YAML boolean; true together with a
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
        if not _is_yaml_bool(pfe):
            return (f"field 'http_config.proxy_from_environment' must be true or "
                    f"false, got {_type_name(pfe)}")
        from_env = _yaml_bool_true(pfe)
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


def receiver_optional_problem(rtype: str, receiver: dict[str, Any]) -> Optional[str]:
    """Check the optional values Alertmanager cannot load (#2295).

    Read from tenant-config.schema.json like the required-field formats:

    - every property of the type referencing ``yamlBool`` (``send_resolved``,
      email ``require_tls``) must be a YAML boolean: true / false, null
      (unset) or a ``yaml_bool_literals()`` word — ``send_resolved: maybe``
      makes Alertmanager refuse the whole config;
    - the property referencing ``httpConfigOrNull`` (webhook ``http_config``)
      follows ``_http_config_problem``.

    Returns the first problem (callers prefix tenant / receiver context), or
    ``None``. Same rule as the Go copy (pkg/receiverspec Check); cases in
    components/threshold-exporter/app/pkg/receiverspec/testdata/receiver_presence_cases.json.
    """
    fields = _receiver_field_schemas().get(rtype, {})
    for field in receiver_bool_fields(rtype):
        if field in receiver and not _is_yaml_bool(receiver[field]):
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
    ``None``. Same rule as the Go copy (pkg/receiverspec
    requiredProblem); cases in
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
