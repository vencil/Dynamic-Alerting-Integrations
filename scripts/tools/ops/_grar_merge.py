"""Routing-config merging + tenant substitution + receiver building.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _apply_timing_params(...)         → group_wait/interval/repeat with guardrails
  _substitute_tenant(obj, name)     → recursive {{tenant}} → name replacement
  _contains_tenant_placeholder(obj) → True if any string contains {{tenant}}
  merge_routing_with_defaults(...)  → shallow merge of defaults + tenant routing
  build_receiver_config(...)        → structured receiver dict → AM config dict
  skipped_entry_warning(line)       → mark a dropped-entry WARN line (#2489)
  replaced_value_warning(line)      → mark a replaced-value WARN line (#2490)
"""
from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    PLATFORM_DEFAULTS,
    validate_and_clamp,
    receiver_exactly_one_problem,
    receiver_optional_problem,
    receiver_bool_fields,
    coerce_yaml_bool,
    receiver_required_problem,
    RECEIVER_TYPES,
)
from _lib_validation import INVALID_DURATION_HINT, am_duration_seconds  # noqa: E402


# ── #2489: "a config entry was dropped as unusable" is a TYPE, not a word ──
# The generation warning stream stays ``list[str]`` and every line reads
# exactly as before; what marks a line blocking under ``--validate`` is that
# it was built by ``skipped_entry_warning``. ``blocking_generation_errors``
# (``_grar_validate``) tests ``isinstance``, never the text: the text carries
# the operator's values, and the substring test it replaces blocked a plain
# clamp WARN whose value happened to be ``skipping``.
# Defined here, not in ``_grar_validate``, because this module is the leaf
# every producer can import (``_grar_validate`` imports this one).
# ⛔ The mark lives on the str OBJECT: any re-formatting between the producer
# and the predicate (an f-string, ``"  " + w``, ``.strip()``, a join, a
# round-trip through text) returns a plain ``str`` and the line silently
# stops blocking. Append / extend / list concatenation keep it.
# ``tests/ops/test_grar_skipped_entry_warning.py`` refuses a ``WARN …,
# skipping`` literal under scripts/tools/ops/ that is not built here.
class SkippedEntryWarning(str):
    """A warning-stream line saying a config entry was dropped as unusable."""

    __slots__ = ()


def skipped_entry_warning(line: str) -> SkippedEntryWarning:
    """Mark *line* (text unchanged) as a dropped-entry line — blocking under
    ``--validate`` and validate-config's ``schema`` / ``routes`` rows."""
    return SkippedEntryWarning(line)


# ── #2490: "a config value was replaced as unusable" is a type too ────────
# A timing value Alertmanager cannot read (``1.5h``, ``30m1h``, ``1ns``) is
# not dropped with its entry — the route still renders, with the platform
# default in its place — so it is not a ``SkippedEntryWarning``: explain-route
# lists those as sub-routes "not in effect", and this route IS in effect.
# But the config as written cannot be used either, so ``--validate`` and
# validate-config refuse it: ``blocking_generation_errors`` counts this type
# as its own category. Same rules as ``SkippedEntryWarning`` — the mark lives
# on the str object and is lost by any re-formatting.
class ReplacedValueWarning(str):
    """A warning-stream line saying a config value was replaced as unusable."""

    __slots__ = ()


def replaced_value_warning(line: str) -> ReplacedValueWarning:
    """Mark *line* (text unchanged) as a replaced-value line — blocking under
    ``--validate`` and validate-config's ``schema`` / ``routes`` rows."""
    return ReplacedValueWarning(line)


def _apply_timing_params(source_dict: dict, context_name: str) -> tuple[dict, list[str]]:
    """Apply timing parameters with guardrails to a route dict.

    Reads group_wait, group_interval, repeat_interval from source_dict,
    validates each against GUARDRAILS, and returns applied values + warnings.

    #2490: a value Alertmanager cannot read (``am_duration_seconds`` is None;
    the syntax is tenant-config.schema.json ``definitions.duration``) is
    rendered as the platform default, and its line is a
    ``ReplacedValueWarning``: rendering continues, ``--validate`` and
    validate-config refuse the config.

    Returns:
        (timing_dict, warnings_list) — timing_dict has clamped param values.
    """
    timing = {}
    warnings = []
    for param in ("group_wait", "group_interval", "repeat_interval"):
        val = source_dict.get(param)
        if val:
            if am_duration_seconds(str(val)) is None:
                default = PLATFORM_DEFAULTS[param]
                warnings.append(replaced_value_warning(
                    f"  WARN: {context_name}: invalid {param} '{val}' "
                    f"({INVALID_DURATION_HINT}), rendering the platform default "
                    f"{default} in its place; generate-routes --validate and "
                    "validate-config refuse this config"))
                timing[param] = default
                continue
            clamped, param_warnings = validate_and_clamp(param, str(val), context_name)
            warnings.extend(param_warnings)
            if clamped:
                timing[param] = clamped
    return timing, warnings


def _substitute_tenant(obj: object, tenant_name: str) -> object:
    """Replace {{tenant}} placeholders in all string values recursively."""
    if isinstance(obj, str):
        return obj.replace("{{tenant}}", tenant_name)
    if isinstance(obj, dict):
        return {k: _substitute_tenant(v, tenant_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_tenant(item, tenant_name) for item in obj]
    return obj


def _contains_tenant_placeholder(obj: object) -> bool:
    """Check if any string value contains {{tenant}} placeholder."""
    if isinstance(obj, str):
        return "{{tenant}}" in obj
    if isinstance(obj, dict):
        return any(_contains_tenant_placeholder(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_tenant_placeholder(item) for item in obj)
    return False


# ── #2326: the routing layer chain across conf.d directory levels ──────
# ADR-017 "Amendment 2026-09-28": `_routing_defaults` comes from the root
# (any root `_` file) and then from the defaults carrier of every directory on
# the tenant's path, each level a SHALLOW merge per top-level key, deeper
# wins; routing profiles are visible to the tenants at their own level or
# below. `level` is a directory relative to the conf.d root, POSIX-spelled,
# "." for the root itself. Go twin: `routingpolicy.Tree.LayersFor`.
ROOT_LEVEL = "."


def chain_levels(level: str) -> list[str]:
    """The directory levels BELOW the root on the way to *level*, root-first.

    `"a/b"` → `["a", "a/b"]`; the root (`"."` or `""`) → `[]`.
    """
    if level in (ROOT_LEVEL, ""):
        return []
    parts = level.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))]


def level_contains(level: str, other: str) -> bool:
    """True when directory *other* is *level* itself or lies below it."""
    if level in (ROOT_LEVEL, ""):
        return True
    return other == level or other.startswith(level + "/")


def domain_policy_levels(parsed: dict) -> list[tuple[str, dict]]:
    """Every level's ``domain_policies``, in the order they are judged.

    #2326 (d): the root's first (``ROOT_LEVEL``), then each subdirectory
    level's in name order. ``load_tenant_tree`` judges them one level at a
    time over the tenants each reaches (``policy_reaches``), and
    ``explain_route --trace`` walks the same list, so the two cannot disagree
    about which policies a tenant meets.
    """
    levels = [(ROOT_LEVEL, parsed.get("domain_policies") or {})]
    levels.extend(sorted((parsed.get("domain_policies_by_dir") or {}).items()))
    return levels


def policy_reaches(level: str, tenant: str, tenant_dirs: dict[str, str]) -> bool:
    """True when a domain policy declared at directory *level* applies to
    *tenant* (#2326 (d)): a root policy reaches every tenant, a subtree
    policy only the tenants whose file sits in its subtree. A tenant no file
    places (absent from *tenant_dirs*) counts as a root tenant."""
    return level_contains(level, tenant_dirs.get(tenant, ROOT_LEVEL))


def resolve_routing_defaults(parsed: dict, level: str,
                             root_defaults: object = None) -> dict:
    """`_routing_defaults` as a tenant in directory *level* sees it.

    The root's value first (*root_defaults*, else ``parsed["routing_defaults"]``),
    then every subdirectory level's on the way down, each top-level key
    replacing the one above it WHOLE — a null included (it is stored; the
    field rules downstream omit or refuse it). A level whose value is not a
    mapping contributes nothing (its reader already said so).
    """
    root = parsed.get("routing_defaults") if root_defaults is None else root_defaults
    base = dict(root) if root else {}
    levels = parsed.get("routing_defaults_levels") or {}
    for d in chain_levels(level):
        rd = levels.get(d)
        if isinstance(rd, dict):
            base.update(rd)
    return base


def visible_routing_profiles(parsed: dict, level: str) -> dict:
    """The routing profiles a tenant in directory *level* can reference:
    those defined at the root, at *level*, or at any level in between.

    A parsed dict built without the per-directory index (a hand-built one in
    a test) falls back to the flat ``routing_profiles`` map.
    """
    by_dir = parsed.get("routing_profiles_by_dir")
    if by_dir is None:
        return parsed.get("routing_profiles", {})
    out: dict = {}
    for d in [ROOT_LEVEL, *chain_levels(level)]:
        out.update(by_dir.get(d, {}))
    return out


def merge_routing_with_defaults(defaults: dict, tenant_routing: dict | None, tenant_name: str) -> dict:
    """Merge _routing_defaults with tenant _routing.

    Rules:
    - Tenant values override defaults (shallow merge)
    - {{tenant}} in string values is replaced with tenant_name
    - Lists (e.g., group_by) are replaced, not concatenated
    """
    merged = dict(defaults)
    if isinstance(tenant_routing, dict):
        for key, value in tenant_routing.items():
            merged[key] = value
    return _substitute_tenant(merged, tenant_name)


def build_receiver_config(receiver_obj: dict, tenant: str) -> tuple[dict | None, list[str]]:
    """Build Alertmanager receiver config from structured receiver object.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for error messages.

    Returns:
        (am_config_dict, warnings) where am_config_dict is e.g.
        {"webhook_configs": [{"url": "..."}]} or None on error.
    """
    warnings = []

    if not isinstance(receiver_obj, dict):
        warnings.append(skipped_entry_warning(f"  WARN: {tenant}: 'receiver' must be an object with 'type', skipping"))
        return None, warnings

    rtype = receiver_obj.get("type")
    if not rtype or not isinstance(rtype, str):
        warnings.append(skipped_entry_warning(f"  WARN: {tenant}: missing required 'receiver.type', skipping"))
        return None, warnings

    # Exact match, no case folding or trimming (#2180): the schema (`const`)
    # and the Go guard both reject `Email` / ` email`, so normalising here
    # made the generator the only one of the three to accept them.
    if rtype not in RECEIVER_TYPES:
        supported = ", ".join(sorted(RECEIVER_TYPES.keys()))
        warnings.append(skipped_entry_warning(f"  WARN: {tenant}: unknown receiver type '{rtype}' "
                                              f"(supported: {supported}), skipping"))
        return None, warnings

    spec = RECEIVER_TYPES[rtype]

    # Validate required fields: presence, type and format by the schema (#2180)
    for field in spec["required"]:
        problem = receiver_required_problem(rtype, receiver_obj, field)
        if problem:
            warnings.append(skipped_entry_warning(f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping"))
            return None, warnings
    problem = receiver_exactly_one_problem(rtype, receiver_obj)
    if problem:
        warnings.append(skipped_entry_warning(f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping"))
        return None, warnings
    # #2295: optional values Alertmanager cannot load (non-boolean
    # send_resolved / require_tls, a malformed http_config) — without this the
    # receiver is written out and only amtool, when it is on PATH, stops it.
    problem = receiver_optional_problem(rtype, receiver_obj)
    if problem:
        warnings.append(skipped_entry_warning(f"  WARN: {tenant}: receiver type '{rtype}' {problem}, skipping"))
        return None, warnings

    # Build AM config — include required + present optional fields
    am_entry = {}
    for field in spec["required"] + spec["optional"]:
        if field in receiver_obj:
            am_entry[field] = receiver_obj[field]

    # Alertmanager's email_config.to is a single STRING (comma-separated for
    # multiple recipients). conf.d authors — and _routing_defaults — conventionally
    # write `to` as a YAML list (one address per line), which is far more readable.
    # Coerce that list into AM's comma-joined string here, at the boundary where we
    # emit AM config: a raw YAML sequence trips Alertmanager's parser with
    # `cannot unmarshal !!seq into string` at config load (amtool check-config),
    # a failure the dict-only Python validation never surfaces.
    # #2295: a YAML 1.1 boolean word that reached us as a string (quoted, or
    # from JSON) is written as the boolean it means: Alertmanager's yaml.v2
    # refuses a quoted 'yes' for a bool field. receiver_optional_problem has
    # already refused every other non-boolean.
    for field in receiver_bool_fields(rtype):
        if field in am_entry:
            am_entry[field] = coerce_yaml_bool(am_entry[field])
    http_config = am_entry.get("http_config")
    if isinstance(http_config, dict) and "proxy_from_environment" in http_config:
        am_entry["http_config"] = {
            **http_config,
            "proxy_from_environment": coerce_yaml_bool(http_config["proxy_from_environment"]),
        }

    if rtype == "email" and isinstance(am_entry.get("to"), list):
        am_entry["to"] = ", ".join(str(addr) for addr in am_entry["to"])

    return {spec["am_key"]: [am_entry]}, warnings
