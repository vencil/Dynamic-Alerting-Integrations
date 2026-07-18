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


def _inhibit_side_matchers(rule: dict, side: str) -> list[str] | None:
    """Normalize a rule's source/target side to a list of matcher strings.

    `side` is "source" or "target". Handles both the current `*_matchers` list
    form and the legacy `*_match` / `*_match_re` map form. Returns None when the
    rule has NO specification for that side at all (malformed — not our concern);
    returns an empty list only when `*_matchers: []` is explicitly a match-all.
    """
    if f"{side}_matchers" in rule:
        return list(rule.get(f"{side}_matchers") or [])
    out: list[str] = []
    has_legacy = False
    for k, v in (rule.get(f"{side}_match") or {}).items():
        out.append(f'{k}="{v}"')
        has_legacy = True
    for k, v in (rule.get(f"{side}_match_re") or {}).items():
        out.append(f'{k}=~"{v}"')
        has_legacy = True
    return out if has_legacy else None


def _inhibit_target_matchers(rule: dict) -> list[str] | None:
    """Target side of an inhibit rule as matcher strings (see _inhibit_side_matchers)."""
    return _inhibit_side_matchers(rule, "target")


def _matchers_gate_label_present(matchers: list[str], label: str) -> bool:
    """Does this matcher set GUARANTEE `label` is present (non-empty)?

    True iff some matcher NAMES `label` and excludes the empty string for it —
    i.e. an alert whose `label` is missing/empty would NOT match. Reuses
    _matcher_matches_labels so the regex/operator semantics are the SAME code
    the Watchdog guard uses: `label=~".+"` and `label="x"` gate; `label=~".*"`
    does not. An unnamed label is not gated by that matcher.
    """
    for m in matchers or []:
        parsed = _INHIBIT_MATCHER_RE.match(m)
        if not parsed or parsed.group(1) != label:
            continue
        if not _matcher_matches_labels(m, {label: ""}):
            return True
    return False


def find_ungated_equal_label_inhibits(
        inhibit_rules: list[dict] | None) -> list[tuple[int, dict, list[str]]]:
    """Return [(index, rule, [ungated_labels]), ...] for every inhibit rule that
    lists an `equal:` label which is presence-gated on NEITHER side.

    Such a label is the PR #1132 footgun: Alertmanager treats a label missing
    from BOTH the source and target alert as EQUAL, so the rule silently
    suppresses unrelated alerts (and dedup dies when the source cannot carry it).
    A label gated on EITHER side (source OR target) is safe — an alert lacking it
    cannot match that side, so the missing==missing comparison never arises.

    Empty result = invariant holds.
    """
    out: list[tuple[int, dict, list[str]]] = []
    for i, rule in enumerate(inhibit_rules or []):
        if not isinstance(rule, dict):
            continue
        equal = rule.get("equal")
        if not isinstance(equal, list):
            continue
        src = _inhibit_side_matchers(rule, "source") or []
        tgt = _inhibit_side_matchers(rule, "target") or []
        ungated = [
            lbl for lbl in equal
            if isinstance(lbl, str)
            and not _matchers_gate_label_present(src, lbl)
            and not _matchers_gate_label_present(tgt, lbl)
        ]
        if ungated:
            out.append((i, rule, ungated))
    return out


def assert_equal_labels_gated(inhibit_rules: list[dict] | None) -> None:
    """Fail-closed guard: raise ValueError if any inhibit rule lists an `equal:`
    label that is presence-gated on neither side (the PR #1132 silent-suppression
    footgun). Run on the FINAL merged inhibit set in --strict render paths.

    Unlike the Watchdog guard (unconditional — a suppressed dead-man's-switch is
    catastrophic), this is invoked only in --strict so a BYO customer's existing
    pipeline degrades to a WARNING rather than hard-breaking on a latent config
    smell; the platform's own CI runs --strict and thus hard-fails."""
    offending = find_ungated_equal_label_inhibits(inhibit_rules)
    if not offending:
        return
    details = "; ".join(
        f"inhibit_rules[{i}] equal={lbls} not presence-gated on either side"
        for i, _r, lbls in offending)
    raise ValueError(
        "#1132 invariant violated: inhibit rule(s) list an equal-label that no "
        f"matcher guarantees present ({details}). Alertmanager treats a label "
        "missing from BOTH source and target as equal, so the rule silently "
        'suppresses unrelated alerts. Fix: gate the label (`<label>=~".+"`) on '
        "source_matchers OR target_matchers (either side satisfies the invariant; "
        "gating both is defence in depth), or remove it from `equal:`.")


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


# ── ADR-007 --strict: blocking-error prefix (single source of truth) ──
# Consumers (generate_alertmanager_routes._policy_errors, validate_config)
# match warning-stream lines on this prefix to decide blocking. A pin test
# in tests/ops/test_generate_alertmanager_routes.py asserts no other
# _grar_* source can emit this prefix into the validate warning stream.
POLICY_ERROR_PREFIX = "ERROR:"

# Prometheus/Go-style duration grammar for domain-policy checks: one or
# more <number><unit> tokens (multi-unit "1h30m", fractional "1.5h") or
# the bare literal "0". Signs are rejected — a negative duration is never
# a valid Alertmanager timing value.
_POLICY_DURATION_UNITS: dict[str, float] = {
    "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3,
    "s": 1.0, "m": 60.0, "h": 3600.0,
    "d": 86400.0, "w": 604800.0, "y": 31536000.0,
}
_POLICY_DURATION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h|d|w|y))+$")
_POLICY_DURATION_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h|d|w|y)")


def _parse_policy_duration(value: object) -> float | None:
    """Parse a duration for domain-policy checks; None if invalid.

    Unlike the shared single-unit ``parse_duration_seconds`` (deliberately
    left untouched — it backs the timing-guardrail clamps and other
    consumers), this parser accepts Prometheus/Go multi-unit forms
    ("1h30m") and fractional units ("1.5h"), and explicitly rejects
    negative values. Bare non-negative numbers are treated as seconds
    (matching the legacy parser's int/float handling).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    s = value.strip()
    if s == "0":
        return 0.0
    if not _POLICY_DURATION_RE.match(s):
        return None
    return sum(float(num) * _POLICY_DURATION_UNITS[unit]
               for num, unit in _POLICY_DURATION_TOKEN_RE.findall(s))


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
            append a fix hint to each violation message, and fail LOUD on
            every malformed input the lenient path silently skips:
            unparseable/negative durations (policy or tenant side),
            non-list receiver-type / enforce_group_by constraints,
            non-mapping policy or constraints blocks, and a non-list
            tenant group_by. The CLI (`generate_alertmanager_routes.py
            --strict`) treats these ERROR lines as blocking (exit 1).
            Non-strict (WARN) message text and skip behavior are
            unchanged for backward compatibility — including the legacy
            quirks (a falsy parsed duration like "0s" or a multi-unit
            "1h30m" is silently skipped there).

    Known limitation: a ``domain_policies:`` block in a wrongly named
    file, or an unparseable ``_domain_policy.yaml``, never reaches this
    function — those are surfaced (strict → ERROR) by
    ``load_tenant_configs`` in ``_grar_parse``.

    Returns list of warning/error messages.
    """
    messages: list[str] = []
    severity = POLICY_ERROR_PREFIX.rstrip(":") if strict else "WARN"

    def _fmt(base: str, hint: str) -> str:
        """Format one violation; strict mode appends the fix hint."""
        msg = f"  {severity}: {base}"
        if strict:
            msg += f" — fix: {hint}"
        return msg

    def _constraint_list(policy_name: str, constraints: dict,
                         field: str) -> list:
        """Fetch a list-typed constraint; strict ERRORs on a wrong type.

        None (explicit null, schema-legal) and absent both mean "not
        constrained". Non-strict keeps the legacy silent-skip outcome.
        """
        raw = constraints.get(field)
        if raw is None:
            return []
        if not isinstance(raw, list):
            if strict:
                messages.append(_fmt(
                    f"domain_policy '{policy_name}': constraint '{field}' "
                    f"must be a list, got {type(raw).__name__} — the "
                    f"constraint cannot be enforced",
                    f"define '{field}' as a YAML list"))
            return []
        return raw

    for policy_name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            # Explicit null policy is schema-legal (inert); anything else
            # non-mapping is fail-open — strict surfaces it.
            if strict and policy is not None:
                messages.append(_fmt(
                    f"domain_policy '{policy_name}': policy must be a "
                    f"mapping, got {type(policy).__name__} — the policy "
                    f"cannot be enforced",
                    "define the policy as a mapping with "
                    "description/tenants/constraints keys"))
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            messages.append(_fmt(
                f"domain_policy '{policy_name}': 'tenants' must be a list",
                "define 'tenants' as a YAML list of tenant ids"))
            continue
        constraints = policy.get("constraints", {})
        if not isinstance(constraints, dict):
            # Explicit null constraints is schema-legal (inert policy).
            if strict and constraints is not None:
                messages.append(_fmt(
                    f"domain_policy '{policy_name}': 'constraints' must be "
                    f"a mapping, got {type(constraints).__name__} — the "
                    f"policy cannot be enforced",
                    "define 'constraints' as a mapping of constraint keys"))
            continue

        forbidden_types = set(_constraint_list(
            policy_name, constraints, "forbidden_receiver_types"))
        allowed_types = set(_constraint_list(
            policy_name, constraints, "allowed_receiver_types"))
        enforce_group_by = _constraint_list(
            policy_name, constraints, "enforce_group_by")
        max_repeat = constraints.get("max_repeat_interval")
        min_group_wait = constraints.get("min_group_wait")

        # Strict: validate constraint-side durations once per policy —
        # an unparseable bound (e.g. "banana", "-1h") means the constraint
        # would never fire, which must be loud, not silent.
        max_sec: float | None = None
        min_sec: float | None = None
        if strict:
            for field, raw in (("max_repeat_interval", max_repeat),
                               ("min_group_wait", min_group_wait)):
                if raw is not None and _parse_policy_duration(raw) is None:
                    messages.append(_fmt(
                        f"domain_policy '{policy_name}': constraint "
                        f"'{field}' value '{raw}' is not a valid duration "
                        f"— the constraint cannot be enforced",
                        "use Prometheus/Go duration syntax such as '30s', "
                        "'1h' or '1h30m'; negative values are not allowed"))
            if max_repeat is not None:
                max_sec = _parse_policy_duration(max_repeat)
            if min_group_wait is not None:
                min_sec = _parse_policy_duration(min_group_wait)

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
            if strict:
                if max_sec is not None:
                    tenant_repeat = rc.get("repeat_interval")
                    if tenant_repeat is not None:
                        tenant_sec = _parse_policy_duration(tenant_repeat)
                        if tenant_sec is None:
                            messages.append(_fmt(
                                f"domain_policy '{policy_name}', "
                                f"tenant '{tenant}': repeat_interval "
                                f"'{tenant_repeat}' is not a valid duration "
                                f"— cannot check against max '{max_repeat}'",
                                "use duration syntax such as '30m' or "
                                "'1h30m'; negative values are not allowed"))
                        elif tenant_sec > max_sec:
                            messages.append(_fmt(
                                f"domain_policy '{policy_name}', "
                                f"tenant '{tenant}': repeat_interval "
                                f"'{tenant_repeat}' exceeds max "
                                f"'{max_repeat}'",
                                f"lower the tenant's repeat_interval to "
                                f"'{max_repeat}' or less, or raise the "
                                f"policy's max_repeat_interval"))
            elif max_repeat:
                # Legacy lenient path — deliberately verbatim (truthiness
                # skips and single-unit parser included) so non-strict
                # output stays byte-identical.
                tenant_repeat = rc.get("repeat_interval")
                if tenant_repeat:
                    legacy_max = parse_duration_seconds(max_repeat)
                    legacy_val = parse_duration_seconds(tenant_repeat)
                    if legacy_max and legacy_val and legacy_val > legacy_max:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"tenant '{tenant}': repeat_interval "
                            f"'{tenant_repeat}' exceeds max '{max_repeat}'",
                            f"lower the tenant's repeat_interval to "
                            f"'{max_repeat}' or less, or raise the policy's "
                            f"max_repeat_interval"))

            # Check min_group_wait
            if strict:
                if min_sec is not None:
                    tenant_gw = rc.get("group_wait")
                    if tenant_gw is not None:
                        tenant_sec = _parse_policy_duration(tenant_gw)
                        if tenant_sec is None:
                            messages.append(_fmt(
                                f"domain_policy '{policy_name}', "
                                f"tenant '{tenant}': group_wait "
                                f"'{tenant_gw}' is not a valid duration "
                                f"— cannot check against minimum "
                                f"'{min_group_wait}'",
                                "use duration syntax such as '30s' or "
                                "'1m30s'; negative values are not allowed"))
                        elif tenant_sec < min_sec:
                            messages.append(_fmt(
                                f"domain_policy '{policy_name}', "
                                f"tenant '{tenant}': group_wait "
                                f"'{tenant_gw}' below minimum "
                                f"'{min_group_wait}'",
                                f"raise the tenant's group_wait to "
                                f"'{min_group_wait}' or more, or lower the "
                                f"policy's min_group_wait"))
            elif min_group_wait:
                # Legacy lenient path — deliberately verbatim (see above).
                tenant_gw = rc.get("group_wait")
                if tenant_gw:
                    legacy_min = parse_duration_seconds(min_group_wait)
                    legacy_val = parse_duration_seconds(tenant_gw)
                    if legacy_min and legacy_val and legacy_val < legacy_min:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"tenant '{tenant}': group_wait "
                            f"'{tenant_gw}' below minimum '{min_group_wait}'",
                            f"raise the tenant's group_wait to "
                            f"'{min_group_wait}' or more, or lower the "
                            f"policy's min_group_wait"))

            # Check enforce_group_by
            if enforce_group_by:
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
                elif strict:
                    messages.append(_fmt(
                        f"domain_policy '{policy_name}', "
                        f"tenant '{tenant}': group_by must be a list, got "
                        f"{type(tenant_gb).__name__} — cannot check "
                        f"enforce_group_by",
                        "define the tenant's group_by as a YAML list of "
                        "label names"))

    return messages
