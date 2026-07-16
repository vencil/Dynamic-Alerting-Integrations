"""URL / domain / schema validation for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _extract_host(value)          → hostname (lowercase) or None
  validate_receiver_domains(...) → SSRF-prevention domain allowlist check
  load_policy(path)             → list of allowed_domains from policy YAML
  validate_tenant_keys(...)      → schema-key typo / unknown-key warnings
  _validate_profile_refs(parsed) → ADR-007 profile-reference existence check
  check_domain_policies(...)    → ADR-007 domain-policy constraint validation
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_python import (  # noqa: E402
    parse_duration_seconds,
    RECEIVER_URL_FIELDS,
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
)


def _extract_host(value: str | None) -> str | None:
    """Extract hostname from a URL or host:port string.

    Returns hostname (lowercase) or None if unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    # host:port format (e.g., smtp.example.com:587)
    if "://" not in value:
        return value.split(":")[0].lower() or None
    parsed = urlparse(value)
    return parsed.hostname


def validate_receiver_domains(receiver_obj: dict, tenant: str, allowed_domains: list[str]) -> list[str]:
    """Validate receiver URL fields against a domain allowlist.

    Args:
        receiver_obj: dict with 'type' and type-specific fields.
        tenant: tenant name for messages.
        allowed_domains: list of allowed domain patterns (fnmatch).

    Returns:
        list of warning strings (empty if all valid).
    """
    warnings = []
    if not allowed_domains or not isinstance(receiver_obj, dict):
        return warnings

    rtype = receiver_obj.get("type", "")
    if isinstance(rtype, str):
        rtype = rtype.strip().lower()

    url_fields = RECEIVER_URL_FIELDS.get(rtype, [])
    for field in url_fields:
        raw = receiver_obj.get(field)
        if not raw:
            continue
        host = _extract_host(raw)
        if not host:
            warnings.append(
                f"  WARN: {tenant}: cannot parse host from receiver "
                f"{field}='{raw}', skipping domain check")
            continue
        if not any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):
            warnings.append(
                f"  WARN: {tenant}: receiver {field} host '{host}' "
                f"not in allowed_domains, skipping")
    return warnings


# ── ADR-025 D1 / #838: Watchdog inhibition-immunity invariant ──────
#
# Alertmanager has NO "exempt from inhibition" primitive — the Watchdog's
# severity:none label only keeps it out of severity-targeted inhibits, it is NOT
# universal immunity (the ADR's explicit warning). The mechanical guarantee is
# instead: no inhibit_rule's target_matchers may match the always-firing Watchdog
# heartbeat — otherwise the heartbeat is suppressed before it leaves Alertmanager
# and the operator's EXTERNAL dead-man's-switch false-alarms "platform dead".
# This validator codifies that guarantee (config-review/lint, not label magic).
#
# The Watchdog alert carries exactly these identifying labels (see
# k8s/03-monitoring/configmap-rules-platform.yaml + _grar_routes._build_watchdog_route).
WATCHDOG_IDENTITY_LABELS = {"alertname": "Watchdog", "severity": "none"}

_INHIBIT_MATCHER_RE = re.compile(r'^\s*([a-zA-Z_]\w*)\s*(=~|!~|!=|=)\s*"?(.*?)"?\s*$')


def _matcher_matches_labels(matcher: str, labels: dict[str, str]) -> bool:
    """Evaluate one Alertmanager matcher string against a concrete label set.

    A matcher we cannot parse conservatively returns True ("could match"), so a
    malformed inhibit rule can never silently slip a Watchdog-suppressing matcher
    past the guard. An invalid regex value is likewise treated as a match.
    """
    m = _INHIBIT_MATCHER_RE.match(matcher)
    if not m:
        return True
    name, op, value = m.group(1), m.group(2), m.group(3)
    actual = labels.get(name, "")
    if op == "=":
        return actual == value
    if op == "!=":
        return actual != value
    if op == "=~":
        try:
            return re.fullmatch(value, actual) is not None
        except re.error:
            return True
    # op == "!~"
    try:
        return re.fullmatch(value, actual) is None
    except re.error:
        return True


def _inhibit_target_matchers(rule: dict) -> list[str] | None:
    """Normalize a rule's target side to a list of matcher strings.

    Handles both the current `target_matchers` list form and the legacy
    `target_match` / `target_match_re` map form. Returns None when the rule has
    NO target specification at all (malformed — not our concern); returns an
    empty list only when `target_matchers: []` is explicitly a match-all.
    """
    if "target_matchers" in rule:
        return list(rule.get("target_matchers") or [])
    out: list[str] = []
    has_legacy = False
    for k, v in (rule.get("target_match") or {}).items():
        out.append(f'{k}="{v}"')
        has_legacy = True
    for k, v in (rule.get("target_match_re") or {}).items():
        out.append(f'{k}=~"{v}"')
        has_legacy = True
    return out if has_legacy else None


def find_watchdog_suppressing_inhibits(inhibit_rules: list[dict] | None) -> list[tuple[int, dict]]:
    """Return [(index, rule), ...] for every inhibit rule whose target side would
    suppress the always-firing Watchdog heartbeat (Alertmanager AND-joins the
    target matchers, so a rule suppresses Watchdog iff ALL its target matchers
    match WATCHDOG_IDENTITY_LABELS; an explicit empty target list is match-all).

    Empty result = invariant holds.
    """
    out: list[tuple[int, dict]] = []
    for i, rule in enumerate(inhibit_rules or []):
        if not isinstance(rule, dict):
            continue
        targets = _inhibit_target_matchers(rule)
        if targets is None:
            continue
        if all(_matcher_matches_labels(m, WATCHDOG_IDENTITY_LABELS) for m in targets):
            out.append((i, rule))
    return out


def assert_watchdog_inhibit_immunity(inhibit_rules: list[dict] | None) -> None:
    """Fail-closed guard: raise ValueError if any inhibit rule would suppress the
    Watchdog heartbeat. Run on the FINAL merged inhibit set at every render path
    so a Watchdog-suppressing rule can never be shipped (ADR-025 D1)."""
    offending = find_watchdog_suppressing_inhibits(inhibit_rules)
    if not offending:
        return
    details = "; ".join(
        f"inhibit_rules[{i}] target="
        f"{r.get('target_matchers', r.get('target_match', r.get('target_match_re')))}"
        for i, r in offending)
    raise ValueError(
        "ADR-025 invariant violated: inhibit rule(s) would suppress the "
        f"always-firing Watchdog heartbeat ({details}). No inhibit_rules "
        'target_matchers may match alertname="Watchdog" — the heartbeat must '
        "always reach the external dead-man's-switch. Remove or narrow the rule "
        "(see the alerting-plane self-liveness runbook).")


def load_policy(policy_path: str | None) -> list[str]:
    """Load policy YAML and return allowed_domains list (may be empty)."""
    if not policy_path or not Path(policy_path).is_file():
        return []
    with open(policy_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    domains = data.get("allowed_domains", [])
    if not isinstance(domains, list):
        return []
    return [d for d in domains if isinstance(d, str)]


# --- ADR-024 Version-Aware Threshold: dimensional `version` label guard ---
# Python mirror of Go config.validateVersionLabel (pkg/config/resolve.go).
# Both sides MUST stay in sync (the ADR's "雙語 da-guard"): the Go side logs
# these at exporter config-load; this Python side surfaces them as da-guard
# schema warnings (escalatable to a reject in CI).
#
# VERSION_LABEL_PATTERN is the Phase-1 baseline and is pilot-calibratable
# (OQ-6): real app.kubernetes.io/version strings may carry uppercase / long
# Git SHAs — widen after pilot observation.
VERSION_LABEL_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"
_VERSION_LABEL_RE = re.compile(VERSION_LABEL_PATTERN)
# Captures the version label inside a dimensional key's {...}: op is "=~"
# (regex) or "=" (exact); group 2 is the quoted value. The `[{,]` anchor
# requires `version` to be a real label name (preceded by `{` or a `,`
# separator), so a substring like `app_version="v2"` is NOT mis-matched.
#
# Known limitation (Gemini adversarial review, #691): this regex is not a
# full PromQL label-set parser, so it MAY false-match if the literal
# `,version="` appears INSIDE another label's quoted string value (e.g.
# `foo_metric{query="...,version=\"x\""}`). The Go side (parseKeyWithLabels,
# a real label-map parse) is immune. Probability is ~0 for threshold keys
# (their values are bare numbers / simple strings, not embedded PromQL), so
# we accept it rather than pull in a parser; this comment is the deliberate
# record that the boundary is understood.
_VERSION_IN_KEY_RE = re.compile(r'[{,]\s*version\s*(=~|=)\s*"([^"]*)"')
# Phase-1 component scope (mirrors Go pilotVersionMetrics = container cpu/memory;
# base metric keys map 1:1 to those component/metric pairs).
PILOT_VERSION_BASE_KEYS = {"container_cpu", "container_memory"}


def _validate_version_label(tenant: str, key: str, base: str) -> list[str]:
    """ADR-024 OQ-6 checks on a dimensional `version` label (advisory)."""
    m = _VERSION_IN_KEY_RE.search(key)
    if not m:
        return []  # no version label on this key
    op, value = m.group(1), m.group(2)
    out: list[str] = []

    if base not in PILOT_VERSION_BASE_KEYS:
        allowed = ", ".join(sorted(PILOT_VERSION_BASE_KEYS))
        out.append(
            f"  WARN: {tenant}: version label on non-pilot metric '{base}' in key "
            f"'{key}' — ADR-024 Phase 1 only permits {allowed}; risks cross-pack "
            f"double-count")

    if op == "=~":
        out.append(
            f"  WARN: {tenant}: regex version matcher in key '{key}' — ADR-024 "
            f"Phase 1 expects an exact version=\"...\" selector")
    elif value == "":
        out.append(
            f"  WARN: {tenant}: empty version label in key '{key}' (ADR-024 OQ-6 "
            f"forbids empty — it collides with the unversioned baseline)")
    elif value == "default":
        out.append(
            f"  WARN: {tenant}: literal version=\"default\" in key '{key}' is "
            f"reserved for the normalize-layer fallback (ADR-024 OQ-6)")
    elif not _VERSION_LABEL_RE.match(value):
        out.append(
            f"  WARN: {tenant}: version '{value}' in key '{key}' violates "
            f"{VERSION_LABEL_PATTERN} (ADR-024 OQ-6; pilot-calibratable)")

    return out


def validate_tenant_keys(tenant: str, keys: set[str], defaults_keys: set[str]) -> list[str]:
    """Check tenant config keys for typos / unknown reserved keys.

    Returns list of warning strings.
    """
    warnings = []
    for key in keys:
        if key in VALID_RESERVED_KEYS:
            continue
        if any(key.startswith(p) for p in VALID_RESERVED_PREFIXES):
            continue
        if key in defaults_keys:
            continue
        # _critical suffix → check base
        if key.endswith("_critical"):
            base = key.removesuffix("_critical")
            if base in defaults_keys:
                continue
        # Dimensional key with {labels}
        if "{" in key:
            base = key.split("{")[0]
            if base in defaults_keys:
                # ADR-024 OQ-6: validate any `version` dimensional label.
                warnings.extend(_validate_version_label(tenant, key, base))
                continue
        # Unknown key
        if key.startswith("_"):
            warnings.append(f"  WARN: {tenant}: unknown reserved key '{key}' (typo?)")
        else:
            warnings.append(f"  WARN: {tenant}: unknown key '{key}' not in defaults")
    return warnings


def _validate_profile_refs(parsed: dict) -> list[str]:
    """Validate that _routing_profile references point to existing profiles.

    v2.1.0 ADR-007.
    Returns list of warning messages.
    """
    warnings: list[str] = []
    profiles = parsed.get("routing_profiles", {})
    refs = parsed.get("tenant_profile_refs", {})
    for tenant, profile_name in sorted(refs.items()):
        if profile_name not in profiles:
            warnings.append(
                f"  WARN: {tenant}: _routing_profile references unknown "
                f"profile '{profile_name}'")
    return warnings


def check_domain_policies(
    routing_configs: dict[str, dict],
    domain_policies: dict[str, dict],
    *,
    strict: bool = False,
) -> list[str]:
    """Validate resolved routing configs against domain policy constraints.

    v2.1.0 ADR-007.

    Args:
        routing_configs: {tenant: resolved_routing_config}
        domain_policies: {policy_name: {tenants, constraints, ...}}
        strict: if True, return ERROR instead of WARN for violations,
            and append a fix hint to each violation message. The CLI
            (`generate_alertmanager_routes.py --strict`) treats these
            ERROR lines as blocking (exit 1). Non-strict (WARN) message
            text is unchanged for backward compatibility.

    Returns list of warning/error messages.
    """
    messages: list[str] = []
    severity = "ERROR" if strict else "WARN"

    def _fmt(base: str, hint: str) -> str:
        """Format one violation; strict mode appends the fix hint."""
        msg = f"  {severity}: {base}"
        if strict:
            msg += f" — fix: {hint}"
        return msg

    for policy_name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            messages.append(f"  WARN: domain_policy '{policy_name}': "
                            "'tenants' must be a list")
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            continue

        forbidden_types = set(constraints.get("forbidden_receiver_types", []))
        allowed_types = set(constraints.get("allowed_receiver_types", []))
        max_repeat = constraints.get("max_repeat_interval")
        min_group_wait = constraints.get("min_group_wait")
        enforce_group_by = constraints.get("enforce_group_by")

        for tenant in tenants:
            if tenant not in routing_configs:
                continue
            rc = routing_configs[tenant]

            # Check receiver type constraints
            recv = rc.get("receiver", {})
            recv_type = recv.get("type", "") if isinstance(recv, dict) else ""
            if recv_type:
                if forbidden_types and recv_type in forbidden_types:
                    messages.append(_fmt(
                        f"domain_policy '{policy_name}', "
                        f"tenant '{tenant}': receiver type '{recv_type}' "
                        f"is forbidden",
                        f"domain forbids {sorted(forbidden_types)}; switch "
                        f"the tenant's receiver.type to a compliant type "
                        f"or amend the domain policy"))
                if allowed_types and recv_type not in allowed_types:
                    messages.append(_fmt(
                        f"domain_policy '{policy_name}', "
                        f"tenant '{tenant}': receiver type '{recv_type}' "
                        f"not in allowed types {sorted(allowed_types)}",
                        f"switch the tenant's receiver.type to one of "
                        f"{sorted(allowed_types)} or amend the domain policy"))

            # Check max_repeat_interval
            if max_repeat:
                tenant_repeat = rc.get("repeat_interval")
                if tenant_repeat:
                    max_sec = parse_duration_seconds(max_repeat)
                    tenant_sec = parse_duration_seconds(tenant_repeat)
                    if max_sec and tenant_sec and tenant_sec > max_sec:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"tenant '{tenant}': repeat_interval "
                            f"'{tenant_repeat}' exceeds max '{max_repeat}'",
                            f"lower the tenant's repeat_interval to "
                            f"'{max_repeat}' or less, or raise the policy's "
                            f"max_repeat_interval"))

            # Check min_group_wait
            if min_group_wait:
                tenant_gw = rc.get("group_wait")
                if tenant_gw:
                    min_sec = parse_duration_seconds(min_group_wait)
                    tenant_sec = parse_duration_seconds(tenant_gw)
                    if min_sec and tenant_sec and tenant_sec < min_sec:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"tenant '{tenant}': group_wait "
                            f"'{tenant_gw}' below minimum '{min_group_wait}'",
                            f"raise the tenant's group_wait to "
                            f"'{min_group_wait}' or more, or lower the "
                            f"policy's min_group_wait"))

            # Check enforce_group_by
            if enforce_group_by and isinstance(enforce_group_by, list):
                tenant_gb = rc.get("group_by", [])
                if isinstance(tenant_gb, list):
                    missing = set(enforce_group_by) - set(tenant_gb)
                    if missing:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"tenant '{tenant}': group_by missing required "
                            f"labels: {sorted(missing)}",
                            f"add {sorted(missing)} to the tenant's group_by "
                            f"(policy requires {sorted(enforce_group_by)})"))

    return messages
