"""URL / domain / schema validation for generate_alertmanager_routes.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  _extract_host(value)          → hostname (lowercase) or None
  validate_receiver_domains(...) → SSRF-prevention domain allowlist check
  load_policy(path)             → list of allowed_domains from policy YAML
                                  (raises PolicyInputError when a path IS
                                  supplied but cannot serve as a policy)
  validate_tenant_keys(...)      → schema-key typo / unknown-key warnings
  _validate_profile_refs(parsed) → ADR-007 profile-reference existence check
  check_domain_policies(...)    → ADR-007 domain-policy constraint validation
"""
from __future__ import annotations

import base64
import binascii
import fnmatch
import json
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout
from _lib_compat import PROJECT_ROOT_MARKERS  # noqa: E402
from _lib_python import (  # noqa: E402
    parse_duration_seconds,
    RECEIVER_URL_FIELDS,
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
)
from _lib_validation import tenant_id_rule  # noqa: E402  (ADR-035)
from _grar_merge import (  # noqa: E402  (#2326 directory scope)
    ROOT_LEVEL,
    SkippedEntryWarning,
    level_contains,
    skipped_entry_warning,
    visible_routing_profiles,
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
                skipped_entry_warning(f"  WARN: {tenant}: cannot parse host from receiver "
                                      f"{field}='{raw}', skipping domain check"))
            continue
        if not any(fnmatch.fnmatch(host, pat) for pat in allowed_domains):
            warnings.append(
                skipped_entry_warning(f"  WARN: {tenant}: receiver {field} host '{host}' "
                                      f"not in allowed_domains, skipping"))
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

    Defensive against malformed shapes (a live Alertmanager's schema forbids them,
    but this runs on customer-supplied config via byo_check): a non-list
    `*_matchers` or non-dict `*_match*` degrades to empty rather than raising, and
    non-string matcher elements are dropped.
    """
    if f"{side}_matchers" in rule:
        matchers = rule.get(f"{side}_matchers")
        return [m for m in matchers if isinstance(m, str)] if isinstance(matchers, list) else []
    out: list[str] = []
    has_legacy = False
    exact = rule.get(f"{side}_match")
    if isinstance(exact, dict):
        for k, v in exact.items():
            out.append(f'{k}="{v}"')
            has_legacy = True
    mre = rule.get(f"{side}_match_re")
    if isinstance(mre, dict):
        for k, v in mre.items():
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


# ── Tenant-scoped silencing must not reach PLATFORM alerts ─────────
#
# Silent Mode (TenantSilentWarning / TenantSilentCritical) is a TENANT-controlled
# switch: a tenant sets `_silent_mode` in its own config and the sentinel fires.
# Its inhibit target is severity + tenant=~".+", which is fine for tenant alerts
# — but THREE platform self-monitoring alerts also carry a `tenant` label and are
# severity=warning, so before the `alert_source=""` matcher was added a tenant
# could mute the platform's own failure alerts. Two of the three
# (FederationRejectionRateAnomaly, FederationGatewayBackendErrors) get `tenant`
# from their expr's `sum by (tenant)`, i.e. only at fire time — reading the rule
# file's `labels:` block says they have no tenant, which is how this survived
# review.
#
# The invariant this codifies is deliberately NARROW: only a rule whose SOURCE
# side is tenant-gated (i.e. it is triggered by something a tenant controls) is
# forbidden from targeting a platform alert. A future deliberate platform→platform
# inhibit (source not tenant-gated) stays legal.
PLATFORM_ALERT_SOURCE_LABEL = "alert_source"
PLATFORM_ALERT_SOURCE_VALUE = "platform"

# Representative label sets of the platform self-monitoring pack
# (k8s/03-monitoring/configmap-rules-platform.yaml). Real alertnames are used so
# a target matcher that names `alertname` is still evaluated fail-closed; the
# repo-anchored test derives the FULL set from the ConfigMap, so drift here
# weakens only the default, never the shipped guarantee.
PLATFORM_ALERT_IDENTITY_LABELS = (
    # the tenant-bearing shapes — the ones this guard exists for
    {"alertname": "FederationGatewayBackendErrors", "severity": "warning",
     "alert_source": "platform", "tenant": "any-tenant"},
    {"alertname": "FederationRejectionRateAnomaly", "severity": "warning",
     "alert_source": "platform", "tenant": "any-tenant"},
    {"alertname": "TenantMetricsOverLimit", "severity": "warning",
     "alert_source": "platform", "tenant": "any-tenant"},
    # tenant-less shapes, one per shipped severity
    {"alertname": "ThresholdExporterAbsent", "severity": "critical",
     "alert_source": "platform"},
    {"alertname": "ThresholdExporterDown", "severity": "warning",
     "alert_source": "platform"},
    {"alertname": "TenantApiReadHANeeded", "severity": "info",
     "alert_source": "platform"},
)


# The shipped platform pack. Deriving identities from it (rather than probing a
# hand-written sample) is what makes the guard fail-closed over alertnames — a
# fixed sample goes stale the moment anyone adds an alert, and it did: the pack
# grew 5 alerts (#1259, #1266) while this PR was open, none of them sampled.
_PLATFORM_RULES_BASENAME = "configmap-rules-platform.yaml"


def _find_platform_rules_configmap() -> "Path | None":
    """Locate the shipped platform pack without counting directory levels.

    ⛔ This used to be a module-scope ``Path(__file__).resolve().parents[3]``.
    That index is only correct for ONE of the two layouts this file ships in,
    and it raised ``IndexError`` at **import** time in the other (#1494): the
    image flattens every tool into ``/opt/da-tools/`` (``build.sh`` copies with
    a bare ``cp <src> tools/``; ``Dockerfile`` ``WORKDIR /opt/da-tools``), which
    leaves only three ancestors. Two module-scope importers
    (``generate_alertmanager_routes``, ``byo_check``) meant the whole
    ``generate-routes`` / ``byo-check`` surface died before its first line.

    ⛔ Counting levels is the defect, so the fix does not count levels — it
    looks for the file. Order matters: flat-first, because in the image the
    pack ships beside this module (``build.sh`` ``REPO_DATA_FILES``, paired to
    this module by ``REQUIRED_DATA_FILES``), while a repo checkout keeps it
    under ``k8s/``. **The flat branch comes first precisely because it assumes
    no marker**: the shipped image is ``python:*-alpine`` with ``WORKDIR
    /opt/da-tools`` and only ``entrypoint.py`` / ``VERSION`` / ``tools/``
    copied in, so none of ``_lib_compat.PROJECT_ROOT_MARKERS`` (``.git`` /
    ``Makefile`` / ``pyproject.toml``) exists anywhere on that ancestor chain
    — nor does ``k8s/``. The image path must therefore resolve before any
    marker is consulted, and the bounded marker walk below serves only the
    repo branch, where a marker does exist. One enumeration, from the shared
    constant: the earlier revision listed the markers twice and the two lists
    disagreed (``k8s`` is not a marker; ``pyproject.toml``, the one a Python
    image is most likely to carry, was missing from the first list).

    Returns None when no copy is reachable — the caller degrades loudly rather
    than raising, because a missing pack must not take the tool down.
    """
    here = Path(__file__).resolve().parent
    flat = here / _PLATFORM_RULES_BASENAME
    if flat.is_file():
        return flat
    # ⛔ BOUNDED at the project root. An unbounded ancestor walk keeps climbing
    # past the checkout, so a stray `k8s/03-monitoring/` anywhere above it —
    # another checkout, a home directory, `/` — would be adopted as this
    # platform's rule pack.
    #
    # ⛔ The marker set is shared with `describe_tenant`, and sharing it is the
    # point: this side was left keyed on `.git` alone for one revision while
    # the other side had already been widened, which made a source tarball
    # (`git archive`, a release zip, a vendored copy — no `.git`) fall back to
    # the 6-entry constant instead of the 41-entry pack. That is the fail-OPEN
    # direction, and it was the MORE serious of the two places, so "fixed the
    # one that was pointed at" left the worse half broken. `.git` is a
    # directory in a clone and a FILE in a worktree, hence `exists()`.
    repo_root = next(
        (base for base in (here, *here.parents)
         if any((base / m).exists() for m in PROJECT_ROOT_MARKERS)),
        None,
    )
    if repo_root is not None:
        candidate = (repo_root / "k8s" / "03-monitoring"
                     / _PLATFORM_RULES_BASENAME)
        if candidate.is_file():
            return candidate
    return None


_PLATFORM_IDENTITY_CACHE: "tuple[dict, ...] | None" = None
_PLATFORM_DEGRADED_WARNED = False


def _warn_probe_set_degraded(reason: str) -> None:
    """Say out loud that the identity probe set fell back to the constant.

    ⛔ The fallback is fail-OPEN (an alert absent from the probe set is one
    :func:`find_tenant_silenceable_platform_inhibits` never tests), and it is
    a 6-entry constant against a 41-entry pack — measured, not estimated. The
    docstring below used to argue the degradation "cannot go unnoticed"
    because a repo-anchored test pins the full set; that test only ever runs
    in a repo layout, so in the image the degradation was precisely unnoticed.
    One line on stderr, once per process, is what makes the claim true.

    ⚠️ The once-per-process flag is ONE boolean for all three degradation
    causes, not one per cause. A run that degrades for a second, different
    reason stays silent about it. That is deliberate for now — a single run
    realistically hits one cause, and a per-cause set would make a noisy path
    noisier — but it means "every degradation path speaks" is true per
    PROCESS, not per CAUSE. Stated because the earlier wording implied the
    latter.
    """
    global _PLATFORM_DEGRADED_WARNED
    if _PLATFORM_DEGRADED_WARNED:
        return
    _PLATFORM_DEGRADED_WARNED = True
    print(
        f"WARN: platform alert identity probe set degraded to the "
        f"{len(PLATFORM_ALERT_IDENTITY_LABELS)}-entry built-in fallback "
        f"({reason}). Tenant inhibit rules that would silence any platform "
        f"alert outside that fallback are NOT checked in this run.",
        file=sys.stderr,
    )
# `sum by (tenant)` / `max by (namespace, tenant)` … — a label the alert only
# carries at fire time, which is exactly the class that made this guard necessary.
#
# ⛔ Matches ANY `by (…)` grouping list, not `<aggregator> by (…)`. PromQL accepts
# the modifier on either side of the argument list, and an aggregator-anchored
# pattern sees only the prefix form:
#     sum by (tenant) (rate(x[5m]))   ← seen
#     sum(rate(x[5m])) by (tenant)    ← MISSED, same query
# A pure reformat between those two — no semantic change, the kind of edit that
# sails through review — used to drop the alert out of this guard's probe set and
# make it tenant-silenceable again. The keyword list was also short three
# aggregators PromQL has (`stddev`, `stdvar`, `quantile`, `count_values`), so
# `stddev by (tenant) (…)` was invisible for no stated reason at all. `by (…)` is
# the whole grammar of the thing being detected; enumerating what may precede it
# only adds ways to be wrong.
_EXPR_TENANT_AGG_RE = re.compile(r'\bby\s*\(\s*[^)]*\btenant\b\s*[,)]')


def _configmap_rule_bodies(doc: dict):
    """Every rule-file body a kubelet would project from *doc*, as text.

    ``data`` values are already text. ``binaryData`` values are base64 and are
    decoded here: a projected ConfigMap volume with explicit ``items`` falls back
    to ``BinaryData`` when the key is absent from ``Data`` (kubelet's
    ``MakePayload``), so a rules file parked there is served to Prometheus
    exactly like a ``data`` one. Undecodable bytes are skipped rather than
    raised on — one unreadable key must not blank the whole probe set, which is
    the fail-open direction.
    """
    for section, decode in (("data", False), ("binaryData", True)):
        for value in (doc.get(section) or {}).values():
            if not decode:
                yield str(value)
                continue
            try:
                yield base64.b64decode(str(value), validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError, binascii.Error):
                continue


def platform_alert_identities(
        configmap_path: "Path | str | None" = None) -> tuple[dict, ...]:
    """Every platform self-monitoring alert's fire-time label identity.

    Rule-level ``labels:`` UNION the labels the expr produces — an alert whose
    expr aggregates ``by (tenant)`` carries a ``tenant`` label at fire time even
    though its ``labels:`` block has none. Reading only the block is precisely
    how a tenant-silenceable platform alert survived review.

    Falls back to :data:`PLATFORM_ALERT_IDENTITY_LABELS` when the ConfigMap is
    unreachable (the tool also runs from images that carry no repo tree). The
    fallback is a strict subset, so it can only under-report, never green-light
    something the full set would flag — and a repo-anchored test pins that
    in-repo callers get the full set, so the degradation cannot go unnoticed.

    ⛔ EVERY ConfigMap document and EVERY key under ``data`` / ``binaryData``,
    not ``docs[0]``'s first key. Both narrowings were silent drops, and dropping
    an identity here is fail-OPEN: an alert absent from the probe set is one
    :func:`find_tenant_silenceable_platform_inhibits` never tests, so a
    tenant-triggered inhibit that would silence it reads as safe. A ConfigMap
    growing a second data key is ordinary (kubelet projects each key as its own
    file and Prometheus globs the directory), and ``binaryData`` is a real
    delivery path in this repo, not a curiosity — see ``_rule_tree`` for the
    kubelet ``MakePayload`` fallback that makes it one.
    """
    global _PLATFORM_IDENTITY_CACHE
    if configmap_path is None and _PLATFORM_IDENTITY_CACHE is not None:
        return _PLATFORM_IDENTITY_CACHE
    if configmap_path:
        path = Path(configmap_path)
    else:
        path = _find_platform_rules_configmap()
        if path is None:
            _warn_probe_set_degraded(
                f"{_PLATFORM_RULES_BASENAME} not found beside this tool nor "
                f"under any ancestor's k8s/03-monitoring/"
            )
            identities = PLATFORM_ALERT_IDENTITY_LABELS
            _PLATFORM_IDENTITY_CACHE = identities
            return identities
    try:
        docs = [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8"))
                if d and d.get("kind") == "ConfigMap"]
        if not docs:
            raise KeyError("no ConfigMap document")
        out: list[dict] = []
        for doc in docs:
            for body in _configmap_rule_bodies(doc):
                rules_doc = yaml.safe_load(body) or {}
                if not isinstance(rules_doc, dict):
                    continue
                for group in rules_doc.get("groups") or []:
                    # ⛔ Per-element isolation, matching `_configmap_rule_bodies`
                    # above. Without it a single non-mapping element raises into
                    # the handler below and `identities` collapses to the
                    # six-entry fallback constant — the fail-OPEN direction this
                    # function's docstring warns about, reachable from one bad
                    # element anywhere in the tree. Measured: 41 identities -> 6.
                    if not isinstance(group, dict):
                        continue
                    for rule in group.get("rules") or []:
                        if not isinstance(rule, dict) or "alert" not in rule:
                            continue
                        labels = dict(rule.get("labels") or {})
                        if labels.get("alert_source") != "platform":
                            # Watchdog rides its own index-0 lane and deliberately
                            # carries no discriminator;
                            # assert_watchdog_inhibit_immunity covers it at the
                            # same call sites. Keying on
                            # the marker (not on the alertname) means a future
                            # unmarked alert is excluded for the same stated reason
                            # rather than by accident.
                            continue
                        labels["alertname"] = rule["alert"]
                        if _EXPR_TENANT_AGG_RE.search(str(rule.get("expr", ""))):
                            labels.setdefault("tenant", "any-tenant")
                        out.append(labels)
        if out:
            identities = tuple(out)
        else:
            _warn_probe_set_degraded(f"{path} yielded no platform alert")
            identities = PLATFORM_ALERT_IDENTITY_LABELS
    except (OSError, yaml.YAMLError, KeyError, StopIteration, AttributeError,
            TypeError, ValueError) as exc:
        _warn_probe_set_degraded(f"{path} unreadable: {type(exc).__name__}")
        identities = PLATFORM_ALERT_IDENTITY_LABELS
    if configmap_path is None:
        _PLATFORM_IDENTITY_CACHE = identities
    return identities


def _pinned_label_values(matchers: list[str], label: str) -> list[str]:
    """Literal values *matchers* pins for *label* via ``label="value"``."""
    out: list[str] = []
    for matcher in matchers or []:
        parsed = _INHIBIT_MATCHER_RE.match(matcher)
        if parsed and parsed.group(1) == label and parsed.group(2) == "=":
            out.append(parsed.group(3))
    return out


def find_tenant_silenceable_platform_inhibits(
        inhibit_rules: list[dict] | None,
        platform_label_sets: "tuple[dict, ...] | list[dict] | None" = None,
) -> list[tuple[int, dict, dict]]:
    """Return [(index, rule, platform_labels), ...] for every TENANT-SCOPED
    inhibit rule whose target side would suppress a platform self-monitoring
    alert.

    "Tenant-scoped" = the SOURCE matchers presence-gate `tenant` (`tenant=~".+"`
    or `tenant="x"`), i.e. the rule can only be triggered by an alert a tenant
    owns. "Would suppress" reuses the same AND-join semantics as the Watchdog
    guard: a rule suppresses an alert iff ALL its target matchers match it.

    Empty result = invariant holds.
    """
    sets = platform_label_sets or platform_alert_identities()
    out: list[tuple[int, dict, dict]] = []
    for i, rule in enumerate(inhibit_rules or []):
        if not isinstance(rule, dict):
            continue
        sources = _inhibit_side_matchers(rule, "source")
        if not sources or not _matchers_gate_label_present(sources, "tenant"):
            continue  # not tenant-triggered → out of scope for this invariant
        targets = _inhibit_target_matchers(rule)
        if targets is None:
            continue
        # A target pinning `tenant="db-a"` cannot be judged against a probe that
        # carries a different tenant value — the equality simply misses and the
        # rule reads as safe. Runtime tenant values are unbounded and cannot be
        # enumerated ahead of time, so take them FROM THE RULE: re-probe every
        # tenant-bearing platform identity with each literal tenant it names.
        probes = list(sets)
        for pinned in _pinned_label_values(targets, "tenant"):
            probes.extend({**labels, "tenant": pinned}
                          for labels in sets if "tenant" in labels)
        for labels in probes:
            if all(_matcher_matches_labels(m, labels) for m in targets):
                out.append((i, rule, labels))
                break
    return out


def assert_platform_alerts_not_tenant_silenceable(
        inhibit_rules: list[dict] | None,
        platform_label_sets: "tuple[dict, ...] | list[dict] | None" = None,
) -> None:
    """Fail-closed guard: raise ValueError if a tenant-triggered inhibit rule
    would suppress a platform self-monitoring alert. Run on the FINAL merged
    inhibit set at every render path, alongside the Watchdog guard."""
    offending = find_tenant_silenceable_platform_inhibits(
        inhibit_rules, platform_label_sets)
    if not offending:
        return
    details = "; ".join(
        f"inhibit_rules[{i}] target="
        f"{r.get('target_matchers', r.get('target_match', r.get('target_match_re')))}"
        f" suppresses {lbls.get('alertname')}"
        for i, r, lbls in offending)
    raise ValueError(
        "Platform-alert silencing invariant violated: tenant-triggered inhibit "
        f"rule(s) would suppress a platform self-monitoring alert ({details}). A "
        "tenant must not be able to mute the platform's own failure alerts. Fix: "
        f'add `{PLATFORM_ALERT_SOURCE_LABEL}=""` to target_matchers (a missing '
        "label equals the empty string in Alertmanager, so tenant alerts still "
        "match while platform alerts are excluded), or narrow the target.")


class PolicyInputError(ValueError):
    """`--policy` was supplied but the value cannot serve as a policy.

    A dedicated subclass rather than a bare ``ValueError`` because two callers
    in validate_config.py already wrap unrelated regions in ``except
    ValueError``; a bare raise here would be swallowed by whichever of those
    happens to grow to enclose the call.
    """


def load_policy(policy_path: str | None) -> list[str]:
    """Load policy YAML and return allowed_domains list (may be empty).

    ⛔ Omitting ``--policy`` and supplying an unusable one are DIFFERENT
    outcomes. Until #1556 both returned ``[]``, so a customer following the
    documented example — ``--policy "webhook.company.com,slack.com"``, which
    names domains rather than a file — got the webhook domain allowlist
    silently switched off while the run printed ``[PASS] policy`` and exited 0.
    dev-rules #13 puts "檔案/路徑不存在" and "malformed 輸入" in
    EXIT_CALLER_ERROR, so a supplied-but-unusable value now raises and the
    callers turn that into exit 2.

    ⛔⛔ NOT CLOSED, and an earlier revision of this docstring said it was.
    It claimed the empty-list return "survives for exactly two inputs, and both
    mean the operator asked for no constraint" — a sentence written in the
    function whose entire purpose is to stop that class. Measured, NINE inputs
    reach ``return []``:

        asked for no constraint (3)   no --policy at all
                                      allowed_domains: []
                                      allowed_domains:        (empty value)
        could not tell (6)            a 0-byte file
                                      a space/newline-only file (⚠️ NOT one
                                        holding a TAB — the YAML scanner
                                        rejects tabs, so that one raises.
                                        Measured; "whitespace-only" was too
                                        wide a word for what was tested.)
                                      a comment-only file
                                      the key absent entirely
                                      the key misspelled (allowed_domain:)
                                      a list whose entries are all non-strings

    The six below the line are the #1556 danger class arriving through a
    different door: the operator supplied a policy, the SSRF domain allowlist
    is off, and the report says ``[PASS] policy``. A truncated ``kubectl cp``, an
    empty ConfigMap key and one missing ``s`` all land there. What this function
    closes is the *path* axis (a value that is not a usable file); the *content*
    axis is open (#1649 — and until that ticket existed this docstring said
    "tracked separately" while nothing tracked it), and must not be read as
    covered because
    the path axis now raises.
    """
    # ⛔ `is None`, not `not policy_path`. An empty string is SUPPLIED — it is
    # what an unset shell variable expands to — and the falsy test routed it
    # into the omitted branch, switching the webhook domain allowlist off at
    # exit 0. Measured on the shipped tree: `--policy ""` produced output
    # byte-identical to omitting the flag. The flag's argparse default is None,
    # so nothing else reaches this branch. Same split as #1616's --base-config.
    if policy_path is None:
        return []
    if not Path(policy_path).is_file():
        raise PolicyInputError(
            f"--policy: not a file: {policy_path!r}\n"
            "  --policy takes a PATH to a policy YAML holding an "
            "`allowed_domains:` list.\n"
            "  ⛔ Do not drop the flag to clear this error — that turns the "
            "webhook domain allowlist off, which is what this error exists "
            "to stop.")
    # ⛔ "Supplied but unusable" is not only "not a file". A file that exists
    # but cannot be decoded or parsed is the same operator error, and the first
    # cut of this function left all three of those escaping as tracebacks with
    # rc=1 — measured, in the PR whose whole subject is that this class must be
    # exit 2. dev-rules #13 files "malformed 輸入" under EXIT_CALLER_ERROR
    # alongside "檔案/路徑不存在"; nothing here may distinguish them.
    try:
        with open(policy_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except UnicodeDecodeError as exc:
        raise PolicyInputError(
            f"--policy: {policy_path!r} is not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyInputError(
            f"--policy: {policy_path!r} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise PolicyInputError(
            f"--policy: cannot read {policy_path!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyInputError(
            f"--policy: top level of {policy_path!r} is "
            f"{type(data).__name__}, expected a mapping with `allowed_domains:`")
    domains = data.get("allowed_domains", [])
    # ⛔ `allowed_domains:` with nothing under it is YAML for an empty value,
    # and it means the same thing as `allowed_domains: []` and as omitting the
    # key: no constraint. An earlier cut of this function raised on it — a
    # REGRESSION against origin/main, and reproducible on this repo's own
    # `.github/custom-rule-policy.yaml` by commenting the entries out during a
    # migration. Worse, the cheapest way to clear that red was to delete the
    # `allowed_domains:` line too, which lands exactly on the silent-off state
    # #1556 exists to abolish. Only a value that is neither a list nor empty
    # is a caller error, because that one cannot be read as "no constraint".
    if domains is None:
        domains = []
    if not isinstance(domains, list):
        raise PolicyInputError(
            f"--policy: `allowed_domains` in {policy_path!r} is "
            f"{type(domains).__name__}, expected a list (or empty for "
            f"no constraint)")
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
    # fullmatch: `$` also succeeds before a trailing newline under `.match`,
    # while the same literal run by Go RE2 (resolve.go) is end-of-text (#1779).
    elif not _VERSION_LABEL_RE.fullmatch(value):
        out.append(
            f"  WARN: {tenant}: version '{value}' in key '{key}' violates "
            f"{VERSION_LABEL_PATTERN} (ADR-024 OQ-6; pilot-calibratable)")

    return out


def is_null_schedule(value: object) -> bool:
    """Go ``nullSchedule`` (pkg/config/null_threshold.go, #2708): a mapping
    whose ``default`` is null, with no override window (``overrides``
    absent, ``[]`` or null) and no key other than ``expires`` / ``reason``.
    It means what a plain null means. A YAML ``<<:`` merge is not covered."""
    if not isinstance(value, dict) or "default" not in value \
            or value["default"] is not None:
        return False
    for k, v in value.items():
        if k in ("default", "expires", "reason"):
            continue
        if k == "overrides" and (v is None or (isinstance(v, list) and not v)):
            continue
        return False
    return True


def writes_nothing(key: object, value: object) -> bool:
    """Go ``nullThreshold`` (#2518, #2708): a threshold key (no ``_``
    prefix) written as null or as a null schedule (``is_null_schedule``) is
    no write. A reserved key's null is ADR-017's delete, not this."""
    if isinstance(key, str) and key.startswith("_"):
        return False
    return value is None or is_null_schedule(value)


def schedule_null_problems(value: object) -> list[str]:
    """Go ``scheduleNullProblems`` (pkg/config/schedule_null.go, #2708):
    each null inside a schedule (a mapping with a ``default`` key) that has
    override windows — a null ``default`` beside them, a null window entry,
    or a window's ``window: null`` / ``value: null``. Empty for anything
    else."""
    if not isinstance(value, dict) or "default" not in value:
        return []
    windows = value.get("overrides")
    if not isinstance(windows, list) or not windows:
        return []
    out = []
    if value["default"] is None:
        out.append(f"`default:` is null beside {len(windows)} override window(s)")
    for i, w in enumerate(windows):
        if w is None:
            out.append(f"`overrides[{i}]` is null")
            continue
        if isinstance(w, dict) and "window" in w and w["window"] is None:
            out.append(f"`overrides[{i}]` has `window: null`")
        if isinstance(w, dict) and "value" in w and w["value"] is None:
            win = w.get("window")
            win_text = json.dumps(win) if isinstance(win, str) else str(win)
            out.append(f"`overrides[{i}]` (window {win_text}) has `value: null`")
    return out


def _canonical_tenant_key(key: str) -> tuple[str, bool]:
    """Canonical spelling for a tenant-config key (#1231 alias boundary).

    Mirrors Go's ``canonicalKeyFor`` (threshold-exporter
    pkg/config/aliases.go): EXACT match against DEPRECATED_KEY_ALIASES,
    plus exactly two structurally-derived shapes — ``<base>_critical`` and
    dimensional ``<base>{...}``. Everything else — including typos that
    merely share the prefix, like ``mysql_cpu_util`` — returns unchanged,
    so they keep failing unknown-key validation. Never prefix-match.
    """
    if key in DEPRECATED_KEY_ALIASES:
        return DEPRECATED_KEY_ALIASES[key], True
    if key.endswith("_critical"):
        base = key.removesuffix("_critical")
        if base in DEPRECATED_KEY_ALIASES:
            return DEPRECATED_KEY_ALIASES[base] + "_critical", True
    brace = key.find("{")
    if brace > 0 and key[:brace] in DEPRECATED_KEY_ALIASES:
        return DEPRECATED_KEY_ALIASES[key[:brace]] + key[brace:], True
    return key, False


def _canonicalize_alias_keys(
        tenant: str, keys: set[str],
        defaults_keys: set[str]) -> tuple[set[str], set[str], list[str]]:
    """#1231 alias pre-pass for validate_tenant_keys.

    Returns ``(keys_view, defaults_view, notices)``: deprecated spellings in
    ``keys`` are replaced by their canonical form (so downstream checks and
    messages name the NEW key), ``defaults_keys`` gets the same canonical
    view (covers the pre-rename state where the platform defaults still
    carry the old spelling), and each aliased key yields one non-blocking
    NOTICE line. When BOTH spellings of the same threshold are present the
    canonical entry wins and the deprecated one is reported as ignored —
    matching the Go resolve boundary's dedup contract.

    Wording contract (pinned by TestDeprecationNoticePin): NOTICE lines
    must not contain the substring "skipping" and must not start with the
    blocking prefix. The prefix is still matched as text (``--strict``
    reads it); the word no longer decides anything for ``--validate`` since
    #2489 — ``blocking_generation_errors`` tests the line's type, and a
    NOTICE is a plain ``str``, not a ``SkippedEntryWarning`` — but it keeps
    an advisory from reading like a dropped entry.
    """
    notices: list[str] = []
    keys_view: set[str] = set()
    for key in sorted(keys):
        canon, was_alias = _canonical_tenant_key(key)
        if not was_alias:
            keys_view.add(key)
            continue
        if canon in keys:
            notices.append(
                f"  NOTICE: {tenant}: deprecated key '{key}' is ignored "
                f"because its replacement '{canon}' is also set — remove "
                f"'{key}' (#1231 rename)")
            continue
        notices.append(
            f"  NOTICE: {tenant}: key '{key}' was renamed to '{canon}' "
            f"(#1231) — the old name still resolves during the 2-release "
            f"transition window; please update this override to '{canon}'")
        keys_view.add(canon)
    defaults_view = {_canonical_tenant_key(k)[0] for k in defaults_keys}
    return keys_view, defaults_view, notices


def validate_tenant_keys(tenant: str, keys: set[str], defaults_keys: set[str],
                         optional_override_keys: set[str] | None = None) -> list[str]:
    """Check tenant config keys for typos / unknown reserved keys.

    Returns list of warning strings. Deprecated key aliases (#1231) emit a
    non-blocking ``NOTICE:`` line INSTEAD of the unknown-key warning: the
    key is canonicalized first (exact-match table, never prefix-match) and
    validated under its canonical spelling against a canonicalized defaults
    view — so an old-spelled ``mysql_cpu_critical`` whose renamed base
    exists is NOT a dangling ``_critical``, while prefix typos keep warning.

    ``optional_override_keys`` (#1189 / TRK-337) is the platform's DECLARED
    surface: keys it recognises but supplies no value for. It is a second
    membership set, deliberately NOT unioned into ``defaults_keys``, because
    the two behave differently downstream and the Go twin
    (``ValidateTenantKeys``) has to reach the same verdict key-for-key.

    ⚠️ Note which way that asymmetry runs. This function's output is
    ADVISORY: ``generate_alertmanager_routes._validate_mode`` only fails on
    dropped-entry lines (``SkippedEntryWarning``, #2489) or on
    ``ERROR:``-prefixed policy lines, and ``unknown key … not in defaults``
    is neither. So a divergence never
    shows up as a red build — it shows up as CI saying nothing at all about a
    config the tenant-api write gate then refuses (or, in the other
    direction, as a missing heads-up). Accepting something Go refuses is
    therefore the dangerous direction, not the safe one. Verdict parity is
    pinned mechanically against a shared table, not by these two suites
    asserting it about each other: ``tests/shared/optional_overrides_membership_matrix.json``.

    * flat / dimensional keys → accepted (Go accepts them too; dimensional
      rows resolve tenant-only, so they emit as soon as they are written)
    * ``<base>_critical`` on a declared base → still WARNS. Go refuses it
      because ``resolveCriticalRows`` keys off ``defaults[base]`` and drops
      the row otherwise; accepting here would mean CI blesses a key the
      exporter silently discards.
    * a ``_critical`` key named ON the list itself → also still WARNS, for
      the same runtime reason. Go's cascade never even reaches its declared
      check for that shape. This is the majority shape: 16 of the registry's
      25 ``tier: optional_overrides`` keys end in ``_critical``.
    """
    warnings = []
    # #1231 alias pre-pass — BEFORE the reserved/defaults checks, mirroring
    # the Go resolve boundary. Reassigning the parameters (rather than
    # rewriting the loop below) keeps the long-standing validation body
    # byte-identical — tests/shared/_mutation_pilot.py pins three of its
    # source snippets verbatim.
    keys, defaults_keys, notices = _canonicalize_alias_keys(
        tenant, keys, defaults_keys)
    warnings.extend(notices)
    # ⛔ Canonicalized by the same table as the defaults view above. A key
    # listed under its retired spelling would never match the canonicalized
    # tenant key, so the platform would be declaring something permanently
    # un-settable with nothing saying so — the silent membership drift this
    # change exists to end, reappearing inside its own fix. Go's twin is
    # canonicalizeOptionalOverrides (pkg/config/aliases.go).
    declared = {_canonical_tenant_key(k)[0]
                for k in (optional_override_keys or ())}
    for key in keys:
        if key in VALID_RESERVED_KEYS:
            continue
        if any(key.startswith(p) for p in VALID_RESERVED_PREFIXES):
            continue
        if key in defaults_keys:
            continue
        # Declared without a platform value → settable, nothing to inherit.
        #
        # ⛔ This check is deliberately FLAT-ONLY, and both exclusions are
        # load-bearing. Go's cascade reaches its `_critical` and dimensional
        # branches BEFORE its declared check and `continue`s out of every arm,
        # so a composite key named on the list never reaches Go's membership
        # widening. Python's cascade has the opposite order, so without these
        # guards a whole-key match here would shadow branches Go runs first:
        #
        #   `<base>_critical` on the list  — Go refuses (resolveCriticalRows
        #       keys off defaults[base] and drops the row when the base has no
        #       value). This is the registry's DOMINANT shape: 16 of the 25
        #       `tier: optional_overrides` keys end in `_critical`.
        #   `<base>{label="v"}` on the list — Go refuses (it looks up the
        #       parsed BASE, never the full dimensional string). Worse, a
        #       whole-key match would `continue` past the dimensional branch
        #       below and silently skip ADR-024 OQ-6 version-label validation
        #       entirely — accepting `{version="bad!!"}` that both sides
        #       otherwise reject.
        #
        # Either shadow puts CI's verdict at odds with the tenant-api write
        # gate: the Py↔Go split of #1189, reappearing inside its own fix.
        if key in declared and not key.endswith("_critical") and "{" not in key:
            continue
        # _critical suffix → check base
        if key.endswith("_critical"):
            base = key.removesuffix("_critical")
            if base in defaults_keys:
                continue
        # Dimensional base may be declared rather than valued. Checked ahead
        # of the defaults-only block below so that block stays byte-identical
        # for the mutation pins; a _critical key never reaches here (no brace).
        if "{" in key and key.split("{")[0] in declared:
            warnings.extend(
                _validate_version_label(tenant, key, key.split("{")[0]))
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
    refs = parsed.get("tenant_profile_refs", {})
    tenant_dirs = parsed.get("tenant_dirs", {})
    origin = parsed.get("routing_profile_origin", {})
    for tenant, profile_name in sorted(refs.items()):
        # #2326: a profile is visible from its own directory level down, so
        # "unknown" is judged against what THIS tenant's chain can see.
        visible = visible_routing_profiles(
            parsed, tenant_dirs.get(tenant, ROOT_LEVEL))
        if profile_name not in visible:
            where = origin.get(profile_name)
            extra = (f" (a profile of that name is defined in {where}, which "
                     f"is not on this tenant's directory chain — a profile is "
                     f"visible only to tenants at its own level or below)"
                     if where else "")
            warnings.append(
                f"  WARN: {tenant}: _routing_profile references unknown "
                f"profile '{profile_name}'{extra}")
    return warnings


def check_policy_scope(
    scope: str,
    domain_policies: dict,
    tenant_dirs: dict[str, str],
    *,
    source: str,
    strict: bool = False,
) -> tuple[list[str], list[tuple[str, str]]]:
    """#2326 (d): a subtree policy may name only tenants of its subtree.

    *scope* is the directory the policy file sits in (never the root: a
    root policy covers every tenant). A `tenants:` entry declared in a tenant
    file OUTSIDE that subtree is a finding — ERROR under ``--strict``, WARN
    otherwise — worded apart from "tenant not found": the tenant exists, the
    policy simply cannot reach it, so the entry is not enforced. A tenant no
    file declares is left to the lint that reports unknown tenants
    (``check_routing_profiles``), as for a root policy.

    Returns ``(messages, [(domain, tenant), ...])``.
    """
    messages: list[str] = []
    rows: list[tuple[str, str]] = []
    # Same two spellings as check_domain_policies' (the --strict consumers
    # select on POLICY_ERROR_PREFIX).
    severity = (POLICY_ERROR_PREFIX if strict else "WARN:").rstrip(":")
    for name, policy in sorted(domain_policies.items()):
        if not isinstance(policy, dict):
            continue
        tenants = policy.get("tenants", [])
        if not isinstance(tenants, list):
            continue
        for t in tenants:
            if not isinstance(t, str):
                continue  # not a tenant id; check_domain_policies names it
            where = tenant_dirs.get(t)
            if where is None or level_contains(scope, where):
                continue
            rows.append((name, t))
            msg = (f"  {severity}: domain_policy '{name}' in {source} names "
                   f"tenant '{t}', which lives outside this policy's subtree "
                   f"{scope}/ (its file is in "
                   f"{'the conf.d root' if where == ROOT_LEVEL else where + '/'})"
                   f" — a policy below the conf.d root applies only to the "
                   f"tenants in its own subtree, so this entry is NOT enforced")
            if strict:
                msg += (f" — fix: move the entry to a _domain_policy.yaml at "
                        f"or above the tenant's directory, or drop '{t}' from "
                        f"this policy")
            messages.append(msg)
    return messages, rows


# ── ADR-007 --strict: blocking-error prefix (single source of truth) ──
# Consumers (generate_alertmanager_routes._policy_errors, validate_config)
# match warning-stream lines on this prefix to decide blocking. A pin test
# in tests/ops/test_generate_alertmanager_routes.py asserts no other
# _grar_* source can emit this prefix into the validate warning stream.
POLICY_ERROR_PREFIX = "ERROR:"

# ── #2279: two generated receivers with one name (blocking in EVERY mode) ──
# Alertmanager refuses a config whose receivers repeat a name, and the tenant
# id has no character set that would make the generated names collision-free
# (`tenant-<t>` for t = `a-route-0` is `tenant-a-route-0`, the name of tenant
# `a`'s routes[0] receiver). ⛔ Deliberately NOT `POLICY_ERROR_PREFIX`: that
# prefix means "an ADR-007 domain-policy finding escalated by --strict", and a
# duplicate name is neither — it blocks with or without --strict. And not a
# `WARN … skipping` line either: nothing was skipped, both receivers are still
# in the list, so wording it as a skip would describe a run that did not happen.
RECEIVER_NAME_COLLISION_PREFIX = "ERROR (duplicate receiver name):"


def is_receiver_name_collision(line: str) -> bool:
    """True for a #2279 duplicate-receiver-name line in the warning stream."""
    return line.lstrip().startswith(RECEIVER_NAME_COLLISION_PREFIX)


# ── #2326: a conf.d tree the routing plane refuses (blocking in EVERY mode) ─
# ADR-017 "Amendment 2026-09-28": `_routing_enforced` below the root, a
# `receiver` / `overrides` written as null in a subdirectory level's
# `_routing_defaults`, one routing-profile name defined in two files. (One
# tenant id declared in two files is `DUPLICATE_TENANT_PREFIX` below, #2315 —
# not a warning-stream line.) The generator exits EXIT_CALLER_ERROR
# before anything is rendered; validate-config's schema row FAILs on it
# (``blocking_generation_errors``). Not `POLICY_ERROR_PREFIX` — none of these
# is a domain-policy finding, and none waits for --strict.
ROUTING_TREE_ERROR_PREFIX = "ERROR (routing tree):"


def is_routing_tree_error(line: str) -> bool:
    """True for a #2326 routing-tree line in the warning stream."""
    return line.lstrip().startswith(ROUTING_TREE_ERROR_PREFIX)
# ── #2315: one tenant id declared by two tenant files (blocking in EVERY mode) ──
# The exporter's walker refuses such a tree WHOLE (`*DuplicateTenantError`,
# pkg/config/tree_scan.go), and so does da-guard. This reader used to merge
# the two blocks key by key and route on whichever file sorted last, rc 0 —
# and da-guard does not run on a change to a tenant file, so nothing in CI
# said so. Not a warning-stream line: the record travels on `TenantTree`, and
# validate-config reports the same state through its own `tenant_uniqueness`
# row (the same scan), so its output is unchanged.
DUPLICATE_TENANT_PREFIX = "ERROR (duplicate tenant):"


def duplicate_tenant_errors(duplicates: "dict[str, list[str]]") -> list[str]:
    """One blocking line per tenant id that more than one tenant file declares.

    *duplicates* is ``_lib_confd.duplicate_declarations``' shape:
    ``{tenant_id: [file, file, ...]}``. Every declaring file is named, because
    which one owns the tenant is the operator's decision — removing the wrong
    one drops that file's overrides silently.
    """
    return [
        f"  {DUPLICATE_TENANT_PREFIX} tenant '{tenant}' is declared in "
        f"{len(files)} files: {', '.join(files)}. The threshold-exporter (and "
        "da-guard) reject the WHOLE config dir in this state, so every tenant "
        "loses alerting, not just this one. Which file owns the tenant is "
        "your decision (removing the wrong one drops its overrides "
        "silently): keep the tenant in exactly one file. If one of these "
        "files cannot be decoded by the threshold-exporter (da-guard exit 3), "
        "fix that file first."
        for tenant, files in sorted(duplicates.items())
    ]


def blocking_generation_errors(warnings: list[str]) -> list[str]:
    """The lines of a generation warning stream that make ``--validate`` fail.

    ⛔ The ONE predicate for "this generation result is unusable", shared by
    ``generate_alertmanager_routes._validate_mode`` and validate-config's
    ``schema`` / ``routes`` rows. #2164 was two spellings of this predicate
    drifting apart (validate-config showed a WARN row at exit 0 while
    ``--validate`` failed); a new blocking category added to one copy only
    reopens exactly that. Three categories today:

    * a config entry was dropped as unusable — a ``SkippedEntryWarning``,
      i.e. a line built by ``skipped_entry_warning`` (#2489). Decided by the
      line's TYPE, never by its text: the text carries the operator's values,
      and the substring test this replaced (``"WARN"`` and ``"skipping"``
      anywhere in the line) blocked a plain clamp WARN whose value was
      ``skipping``. ⛔ The type survives append / extend / list concatenation
      only — a caller that re-formats a line before passing it here turns a
      blocking line into a non-blocking one;
    * a duplicate generated receiver name (#2279);
    * a conf.d tree the routing plane refuses (#2326,
      ``ROUTING_TREE_ERROR_PREFIX``).

    ADR-007 ``--strict`` policy errors are NOT in here: they are blocking only
    under ``--strict`` and each caller already selects them by
    ``POLICY_ERROR_PREFIX``.
    """
    return [w for w in warnings
            if isinstance(w, SkippedEntryWarning)
            or is_receiver_name_collision(w)
            or is_routing_tree_error(w)]


def receiver_name_collisions(labelled: list[tuple[str, str]]) -> list[str]:
    """One blocking line per receiver name that more than one source generates.

    *labelled* is ``[(receiver_name, source), ...]`` in generation order;
    *source* names where the receiver came from, e.g. ``tenant 'a' routes[0]``.
    Every source of a repeated name is named, so the operator sees both sides
    without having to reverse-engineer them from the name — the name is the
    one thing that is ambiguous here.
    """
    by_name: dict[str, list[str]] = {}
    for name, source in labelled:
        by_name.setdefault(name, []).append(source)
    lines = []
    for name, sources in by_name.items():
        if len(sources) < 2:
            continue
        lines.append(
            f"  {RECEIVER_NAME_COLLISION_PREFIX} receiver '{name}' is generated "
            f"by {' and by '.join(sources)}. Alertmanager refuses a config "
            "whose receivers repeat a name (and a merge that de-duplicates by "
            "name would silently keep only one of them). Rename one of the "
            "tenants, or remove one of the entries.")
    return lines

# ── #1231: deprecated tenant-config key aliases ──
# Python mirror of the Go alias boundary (threshold-exporter
# pkg/config/aliases.go `deprecatedKeyAliases`): during the 2-release
# transition window the OLD spelling keeps validating — as its canonical
# key — and emits a non-blocking NOTICE line instead of the unknown-key
# warning (see _canonical_tenant_key / _canonicalize_alias_keys above).
# SSOT note: the alias SSOT is the registry's deprecated_aliases section
# (rule-packs/threshold-registry.yaml, authored in _registry_lib.py
# DEPRECATED_KEY_ALIASES). This dict is a runtime mirror, PINNED to that
# section by tests/lint/test_check_threshold_registry.py
# (test_python_alias_mirror_pinned_to_registry) — a drifted mirror fails
# CI. To open/close an alias window: edit the authored table, regen the
# registry, then update this mirror (and the Go one) to match.
DEPRECATED_KEY_ALIASES = {
    # #944 / #1231: the metric measures mysql threads_running saturation,
    # never host CPU% — the poisoned name is being retired.
    "mysql_cpu": "mysql_threads_running",
}

_LEGACY_BY_CANONICAL = {canon: legacy for legacy, canon in DEPRECATED_KEY_ALIASES.items()}


def _legacy_tenant_key(key: str) -> "str | None":
    """Go `legacySpellingFor`: the deprecated spelling of a canonical key
    (exact, `_critical`-suffixed, dimensional), or None."""
    if key in _LEGACY_BY_CANONICAL:
        return _LEGACY_BY_CANONICAL[key]
    if key.endswith("_critical"):
        base = key.removesuffix("_critical")
        if base in _LEGACY_BY_CANONICAL:
            return _LEGACY_BY_CANONICAL[base] + "_critical"
    brace = key.find("{")
    if brace > 0 and key[:brace] in _LEGACY_BY_CANONICAL:
        return _LEGACY_BY_CANONICAL[key[:brace]] + key[brace:]
    return None


def _other_tenant_key_spellings(key: object) -> "list[str]":
    """Go `otherSpellings`: every spelling of `key`'s threshold but `key`.
    Empty for a key no alias touches — every routing / reserved key."""
    if not isinstance(key, str):
        return []  # a non-text YAML key names no aliased threshold
    canon, _ = _canonical_tenant_key(key)
    out = [canon] if canon != key else []
    legacy = _legacy_tenant_key(canon)
    if legacy is not None and legacy != key:
        out.append(legacy)
    return out


def drop_shadowed_spellings(m: dict) -> dict:
    """Go `dropShadowedSpellings` / resolve's canonical-wins dedup inside
    ONE layer: a deprecated spelling whose canonical spelling the same map
    also sets is dropped (a lone deprecated spelling is kept, not renamed).
    Returns `m` itself when nothing is dropped."""
    drop = [k for k in m if isinstance(k, str)
            and _canonical_tenant_key(k)[1] and _canonical_tenant_key(k)[0] in m]
    if not drop:
        return m
    return {k: v for k, v in m.items() if k not in drop}


def overlay_across_spellings(dst: dict, src: dict) -> None:
    """Go `overlayAcrossSpellings` (#2368): `src` over `dst` per THRESHOLD,
    not per spelling — a key `src` writes also drops from `dst` every other
    spelling of it that `src` does not write itself. For a key no alias
    touches (every routing and reserved key) this is exactly
    ``dst.update(src)``."""
    for k, v in src.items():
        for s in _other_tenant_key_spellings(k):
            if s not in src:
                dst.pop(s, None)
        dst[k] = v

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


# ── ADR-007 label-match `routes` entries (#2245) ──
# Keys a `routes` entry may carry. Anything else (`continue`, `match_re`,
# `matchers`, a typo) changes what the author expects the route to do, and
# the generator does not render it — so the entry is skipped LOUDLY instead
# of being rendered as something narrower or broader than written.
ROUTE_ENTRY_KEYS = frozenset({"match", "receiver", "group_by", "group_wait",
                              "group_interval", "repeat_interval"})

# Prometheus / Alertmanager classic label-name grammar — the same pattern as
# the schema's `routingRoute.match.propertyNames`.
# fullmatch, never match(): `$` also matches before a final "\n", so
# match() took "team\n" for a label name that Go's labelNameRE refuses
# (#2431 review F2).
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def _quote_matcher_value(value: str) -> str:
    """Double-quote *value* for an Alertmanager matcher string.

    AM parses ``label="value"`` with Go-style escapes, so a backslash, a
    double quote or a newline inside the value must be escaped or the
    matcher either fails to parse or matches a different string.
    """
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"')
               .replace("\n", "\\n"))
    return f'"{escaped}"'


def route_entry_matchers(entry: object, idx: int,
                         tenant: str) -> tuple[list[str] | None, list[str]]:
    """Equality matchers for one ``routes[idx]`` entry; ``None`` if unusable.

    The ONE structural predicate for a ``routes`` entry: the generator
    (``_grar_routes.expand_routing_routes``) renders exactly the entries this
    accepts (receiver content aside), and ``list_tenant_subroutes`` lists
    exactly those for the domain-policy checks — so the two cannot disagree
    about which sub-routes exist.

    Only ``{label: value}`` equality is supported (#2245). The list carries
    ONLY this entry's own labels — the ``tenant="<id>"`` matcher stays on the
    parent route (the tenant's main route), exactly as for overrides (#2252).
    Returns ``(None, [WARN … skipping])`` for a non-mapping entry, an
    unsupported key, or a missing / empty / malformed ``match``.
    """
    ctx = f"{tenant}: routes[{idx}]"
    if not isinstance(entry, dict):
        return None, [skipped_entry_warning(f"  WARN: {ctx} must be a dict, skipping")]
    unsupported = sorted(str(k) for k in entry if k not in ROUTE_ENTRY_KEYS)
    if unsupported:
        return None, [
            skipped_entry_warning(f"  WARN: {ctx} has unsupported key(s) {unsupported} (supported: "
                                  f"{sorted(ROUTE_ENTRY_KEYS)}; label equality only — no regex, no "
                                  "continue), skipping")]
    match = entry.get("match")
    if not isinstance(match, dict) or not match:
        return None, [skipped_entry_warning(f"  WARN: {ctx} needs a non-empty 'match' mapping of "
                                            "label: value (an empty match would take every alert "
                                            "of the tenant), skipping")]
    matchers = []
    for label, value in match.items():
        if not isinstance(label, str) or not _LABEL_NAME_RE.fullmatch(label):
            return None, [skipped_entry_warning(f"  WARN: {ctx}: match label {label!r} is not a "
                                                "valid label name, skipping")]
        if not isinstance(value, str):
            return None, [skipped_entry_warning(f"  WARN: {ctx}: match value for '{label}' must be "
                                                f"a string, got {type(value).__name__} {value!r} "
                                                "(quote it in YAML), skipping")]
        if value == "":
            # AM reads label="" as "label absent", so this child would take
            # nearly every alert of the tenant — the same shadowing an empty
            # `match` causes.
            return None, [skipped_entry_warning(f"  WARN: {ctx}: match value for '{label}' is "
                                                "empty (it would match every alert without that "
                                                "label), skipping")]
        matchers.append(f"{label}={_quote_matcher_value(value)}")
    return matchers, []


# #2431: the override keys the generator formats into the override route's
# matcher (``_grar_routes._build_override_matchers``).
OVERRIDE_MATCHER_KEYS = ("alertname", "metric_group")


def routing_values_not_string(routing_config: object) -> list[tuple[str, object]]:
    """``(field, value)`` per matcher value that is not a YAML string (#2431).

    ⛔ THE predicate for the values the generator formats into a matcher and
    so needs as text: ``overrides[i].alertname`` / ``metric_group`` when the
    key is written (null included) and the value of every label-named key of
    a ``routes[i].match`` mapping (a key that is no label name makes the
    entry skipped by ``route_entry_matchers``; its value is not judged). An
    unquoted YAML 1.1 word is not text to PyYAML — ``yes`` is
    True, ``1:30`` is 90, ``2001-12-15`` a date, ``~`` None — and yaml.v3
    (da-guard, tenant-api) reads the same bytes as strings, so the author's
    intent is ambiguous: ``--strict`` refuses it and asks for quotes. The Go
    copy is ``routingpolicy.ValuesNotString``; the parity matrix pins both.

    ``field`` is the path in the routing (``routes[0].match.team``,
    ``overrides[1].alertname``). Order: overrides, then routes, each in list
    order, match labels in source order.
    """
    out: list[tuple[str, object]] = []
    if not isinstance(routing_config, dict):
        return out
    overrides = routing_config.get("overrides")
    if isinstance(overrides, list):
        for idx, override in enumerate(overrides):
            if not isinstance(override, dict):
                continue
            for key in OVERRIDE_MATCHER_KEYS:
                if key in override and not isinstance(override[key], str):
                    out.append((f"overrides[{idx}].{key}", override[key]))
    routes = routing_config.get("routes")
    if isinstance(routes, list):
        for idx, entry in enumerate(routes):
            match = entry.get("match") if isinstance(entry, dict) else None
            if not isinstance(match, dict):
                continue
            for label, value in match.items():
                # A key that is no label name makes route_entry_matchers
                # skip the entry (its own WARN); its value is not judged
                # (#2431 review F1).
                if not (isinstance(label, str) and _LABEL_NAME_RE.fullmatch(label)):
                    continue
                if not isinstance(value, str):
                    out.append((f"routes[{idx}].match.{label}", value))
    return out


def value_not_string_message(tenant: str, field: str, value: object) -> str:
    """The ``--strict`` refusal for one ``routing_values_not_string`` entry."""
    key = field.rsplit(".", 1)[-1]
    return (f"tenant '{tenant}': {field} must be a string, got "
            f"{type(value).__name__} {value!r} — quote it in YAML (e.g. "
            f"{key}: \"...\") so the route generator reads it as text")


# #2503: Alertmanager's group_by wildcard (group by every label).
GROUP_BY_WILDCARD = "..."


def group_by_problems(group_by: list) -> tuple[list[str], list[tuple[int, str, object]]]:
    """``(kept, problems)`` for one ``group_by`` list (#2503).

    ⛔ THE group_by predicate, measured against Alertmanager v0.34.1 with its
    default (UTF-8) label validation — the deployed manifest sets no feature
    flag. AM refuses the WHOLE config for an empty label name, a repeated
    non-wildcard label or ``...`` mixed with labels, and ACCEPTS a YAML
    ``8`` / ``true``, grouping by a label literally named ``"8"`` /
    ``"true"`` — which is not what an unquoted ``8`` / ``on`` (PyYAML: int /
    bool) was meant to be. A repeated ``...`` alone (``['...', '...']``) is
    accepted (its repeat check skips the wildcard), so it is neither a
    finding nor repaired. In this order, each element is judged by its
    original index:

    1. not a ``str`` (as PyYAML reads it) → ``not_string``; ``""`` →
       ``empty``. No label-name regex: AM's UTF-8 validation takes any
       non-empty string (a quoted ``"8"``, ``"a b"``).
    2. a non-wildcard string already kept → ``duplicate`` (compared as
       strings).
    3. every ``...`` while some other label remains → ``wildcard_mixed``.

    ``kept`` is the list the generator renders without ``--strict`` (every
    problem element dropped; ``[]`` means "render no group_by"). ``problems``
    is ``(index, kind, value)`` in index order. The Go copy is
    ``routingpolicy.GroupByProblems``; the parity matrix pins both.
    """
    problems: list[tuple[int, str, object]] = []
    kept: list[tuple[int, str]] = []
    seen: set[str] = set()
    for idx, value in enumerate(group_by):
        if not isinstance(value, str):
            problems.append((idx, "not_string", value))
        elif value == "":
            problems.append((idx, "empty", value))
        elif value in seen and value != GROUP_BY_WILDCARD:
            problems.append((idx, "duplicate", value))
        else:
            seen.add(value)
            kept.append((idx, value))
    if GROUP_BY_WILDCARD in seen and len(seen) > 1:
        problems.extend((idx, "wildcard_mixed", value) for idx, value in kept
                        if value == GROUP_BY_WILDCARD)
        kept = [(idx, value) for idx, value in kept if value != GROUP_BY_WILDCARD]
    problems.sort(key=lambda p: p[0])
    return [value for _idx, value in kept], problems


def routing_group_by_invalid(routing_config: object) -> list[tuple[str, str, object]]:
    """``(field, kind, value)`` per bad ``group_by`` element of a routing (#2503).

    The lists judged are every one the generator renders from a tenant's
    resolved routing: the main route's ``group_by``, then each mapping
    entry's of ``overrides``, then of ``routes`` (list order). A non-list
    ``group_by`` is not judged (the generator renders none). ``field`` is
    the element's path (``group_by[1]``, ``overrides[0].group_by[2]``).
    ``_routing_enforced.group_by`` goes through ``group_by_problems`` at the
    call site. The Go copy is ``routingpolicy.GroupByInvalid``.
    """
    out: list[tuple[str, str, object]] = []
    if not isinstance(routing_config, dict):
        return out

    def judge(prefix: str, holder: dict) -> None:
        group_by = holder.get("group_by")
        if isinstance(group_by, list):
            out.extend((f"{prefix}group_by[{idx}]", kind, value)
                       for idx, kind, value in group_by_problems(group_by)[1])

    judge("", routing_config)
    for key in ("overrides", "routes"):
        entries = routing_config.get(key)
        if isinstance(entries, list):
            for idx, entry in enumerate(entries):
                if isinstance(entry, dict):
                    judge(f"{key}[{idx}].", entry)
    return out


def group_by_problem_text(field: str, kind: str, value: object) -> str:
    """One bad group_by element, for the ``--strict`` ERROR and the WARN.

    ⚠️ Wording: no "domain" / "allowlist" / "blocked" (validate-config's
    policy row classifies on those) and no "must be a string, got" (the
    #2431 line's parse key).
    """
    if kind == "not_string":
        return (f"{field} is {type(value).__name__} {value!r}, not a string — "
                "quote it in YAML (e.g. \"8\", \"on\") so it is a label name; "
                "Alertmanager would group by a label named after its text")
    if kind == "empty":
        return (f"{field} is an empty string — Alertmanager refuses an empty "
                "label name; remove it")
    if kind == "duplicate":
        return (f"{field} repeats label '{value}' listed earlier — "
                "Alertmanager refuses a repeated non-wildcard group_by label; "
                "remove it")
    return (f"{field} is '...' alongside other labels — Alertmanager refuses "
            "the wildcard mixed with labels; keep ['...'] alone or list only "
            "the labels")


def _shape_of(value: object) -> str:
    """``<type> <repr>`` of a YAML value as PyYAML read it, repr capped."""
    if value is None:
        return "null"
    kind = {type(None): "null", bool: "boolean", str: "string",
            list: "list"}.get(type(value), type(value).__name__)
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f"{kind} {text}"


def routing_not_mapping_text(value: object) -> str:
    """#2341 R5: why a tenant's ``_routing`` cannot be read.

    ⚠️ Wording: like ``group_by_problem_text`` — no "domain" / "allowlist" /
    "blocked", no "must be a string, got".
    """
    text = ("_routing must be a mapping or a disabling string (disable, "
            f"disabled, off, false), got {_shape_of(value)}")
    if isinstance(value, bool):
        text += (" — an unquoted true / false / on / off / yes / no is a YAML "
                 "boolean, not a string; to turn routing off write a quoted "
                 "string ('off') or disable")
    return text


def routing_not_mapping_warning(tenant: str, value: object) -> str:
    """The render-mode line (blocking under ``--validate``)."""
    return (skipped_entry_warning(f"  WARN: {tenant}: {routing_not_mapping_text(value)} — no route "
                                  "is rendered for this tenant, skipping"))


def invalid_tenant_id_text(tenant: object) -> str:
    """#2341 R8: a tenant id the routing plane refuses (is_valid_tenant_id).

    The id is shown as a repr so an empty or blank one is visible. The rule
    is cited from its one copy (ADR-035: the schema's
    ``definitions.tenantId.description``), never restated here. ⚠️ Wording
    as ``routing_not_mapping_text``.
    """
    return (f"tenant id {str(tenant)!r} is not a valid tenant id "
            f"({tenant_id_rule()[1]}) — no route, receiver or inhibit rule is "
            "rendered for it")


def routing_defaults_not_mapping_text(fname: str, value: object) -> str:
    """#2341 R5: a ``_routing_defaults`` that is neither a mapping nor null."""
    return (f"_routing_defaults in {fname} must be a mapping, got "
            f"{_shape_of(value)} — this level contributes nothing to the "
            "routing of the tenants it reaches")


# Keys an override route inherits from the tenant's main route when it does
# not declare them itself (#2252): the generator nests every override route
# under the tenant route, and Alertmanager's ``dispatch/route.go`` ``newRoute``
# starts a child from its parent's ``RouteOpts``. ``receiver`` is not here —
# an override without one is never rendered.
SUBROUTE_INHERITED_KEYS = ("group_wait", "group_interval", "repeat_interval",
                           "group_by")


def _renders_on_route(key: str, value: object) -> bool:
    """Whether the generator writes ``key: value`` onto a route.

    Mirrors WHETHER ``_grar_merge._apply_timing_params`` emits a timing key
    (when truthy) and ``_grar_routes`` emits ``group_by`` (a non-empty list)
    — not the emitted VALUE: guardrail clamping is not modelled, matching
    the main route's own check, which also reads the unclamped value.
    """
    if key == "group_by":
        # #2503: what renders is the repaired list; emptied → none rendered.
        return isinstance(value, list) and bool(group_by_problems(value)[0])
    return bool(value)


def list_tenant_subroutes(
        routing_config: dict) -> list[tuple[str, str, dict, frozenset[str]]]:
    """List the sub-routes a tenant's resolved routing emits besides its main route.

    Each entry is ``(ref, match, config, inherited)``: ``ref`` / ``match``
    name the sub-route in operator messages (``override[0]`` /
    ``alertname=X``), and ``config`` is the mapping of that sub-route's
    EFFECTIVE ``receiver``, ``group_wait`` / ``group_interval`` /
    ``repeat_interval`` and ``group_by`` — the same keys, in the same shape,
    as the tenant's main routing config, so a check written against the main
    route applies to a sub-route unchanged (#2243). ``inherited`` names the
    keys in ``config`` that came from the tenant's main routing rather than
    from the sub-route itself, so a message can say where to fix the value.

    Two sources, in the generator's render order: ``_routing.overrides``
    (``_grar_routes.expand_routing_overrides``, ref ``override[<i>]``), then
    the ADR-007 label-match ``routes`` (``expand_routing_routes``, ref
    ``routes[<i>]``, #2245) — whether they came from a routing profile or
    the tenant. Any other kind of sub-route that renders its own
    Alertmanager receiver belongs here too.

    Only entries that can render a route are listed, mirroring the
    generator's structural skips: none when the tenant has no main
    ``receiver`` (the whole tenant is skipped), a non-list ``overrides`` /
    ``routes``, a non-mapping entry, an override without exactly one of
    ``alertname`` / ``metric_group``, a ``routes`` entry
    ``route_entry_matchers`` rejects, or either kind without a
    ``receiver``. Receiver *content* (unknown type, missing fields, domain
    allowlist) is not pre-judged — the main route's check does not
    pre-judge it either.

    #2252: a value the sub-route does NOT declare is back-filled from the
    tenant's main routing, because that is what Alertmanager uses — the
    generator renders every override and ``routes`` entry as a CHILD of the
    tenant's main route, and a child inherits its parent's timing and
    ``group_by``. A key counts as declared when it is truthy (the
    generator's own emit test).
    A truthy but malformed value (e.g. a non-list ``group_by``, which the
    generator drops) is still treated as the sub-route's own, so strict mode
    reports it instead of the tenant's value papering over it. The tenant's
    value is used only when the tenant route actually renders it
    (``_renders_on_route``); otherwise the sub-route's value is left as
    written (both routes then inherit the root's value).
    """
    if not isinstance(routing_config, dict) or not routing_config.get("receiver"):
        return []

    def _effective(sub: dict) -> tuple[dict, frozenset[str]]:
        """*sub* with undeclared inheritable keys back-filled (#2252)."""
        effective = dict(sub)
        inherited = set()
        for key in SUBROUTE_INHERITED_KEYS:
            if sub.get(key):
                continue  # declared: the sub-route's own value, even malformed
            main_value = routing_config.get(key)
            if _renders_on_route(key, main_value):
                effective[key] = main_value
                inherited.add(key)
        return effective, frozenset(inherited)

    subroutes: list[tuple[str, str, dict, frozenset[str]]] = []
    overrides = routing_config.get("overrides")
    for idx, override in enumerate(
            overrides if isinstance(overrides, list) else []):
        if not isinstance(override, dict):
            continue
        alertname = override.get("alertname")
        metric_group = override.get("metric_group")
        if bool(alertname) == bool(metric_group):
            continue
        if not override.get("receiver"):
            continue
        match = (f"alertname={alertname}" if alertname
                 else f"metric_group={metric_group}")
        subroutes.append((f"override[{idx}]", match, *_effective(override)))

    # #2245: ADR-007 label-match `routes`, rendered after the overrides.
    # Same structural predicate as the generator (route_entry_matchers).
    routes = routing_config.get("routes")
    for idx, entry in enumerate(routes if isinstance(routes, list) else []):
        matchers, _warnings = route_entry_matchers(entry, idx, "")
        if matchers is None or not entry.get("receiver"):
            continue
        match = ",".join(f"{k}={v}" for k, v in entry["match"].items())
        subroutes.append((f"routes[{idx}]", match, *_effective(entry)))
    return subroutes


# ── ADR-007 `require_critical_escalation` (#2244) ──
# Receiver types that count as escalation targets for severity="critical".
# ADR-007's finance case asks for "critical → PagerDuty"; a different type,
# or a different target of the same type, is not an escalation.
ESCALATION_TYPES = frozenset({"pagerduty"})


def _subroute_receiver_type(sub_rc: dict) -> str:
    recv = sub_rc.get("receiver")
    return recv.get("type", "") if isinstance(recv, dict) else ""


class EscalationFindings(NamedTuple):
    """Result of ``critical_escalation_findings`` for one tenant."""

    # First escalation destination ("routes[<j>] (<match>)" or "the main
    # receiver"); None ⇔ non-compliant.
    target: str | None
    # (ref, match, receiver_type, caught) of each non-escalation destination
    # that can receive a severity=critical alert, in render order: rendered
    # sub-routes by their ref / match, then the main receiver as
    # ("the main receiver", "", type, "severity=critical"). ``caught`` is
    # the label set such an alert carries (``severity=critical, team=app``).
    leaks: list[tuple[str, str, str, str]]


def _subroute_match(ref: str, routing_config: dict) -> dict:
    """The equality ``match`` a rendered sub-route adds under the tenant route.

    ``override[<i>]`` → ``{alertname|metric_group: str(value)}`` — the
    generator formats the value into the matcher, so ``alertname: 123``
    renders as ``"123"`` and must compare equal to a route's ``"123"``;
    ``routes[<i>]`` → its raw ``match`` (``route_entry_matchers`` already
    requires string values). Only call it with refs from
    ``list_tenant_subroutes`` (they are already structurally valid).
    """
    if ref.startswith("override["):
        override = routing_config["overrides"][int(ref[len("override["):-1])]
        key = "alertname" if override.get("alertname") else "metric_group"
        return {key: str(override[key])}
    return dict(routing_config["routes"][int(ref[len("routes["):-1])]["match"])


def critical_escalation_findings(routing_config: dict,
                                 tenant: str | None = None) -> EscalationFindings:
    """Judge one tenant's resolved routing against ``require_critical_escalation``.

    Compliant ⇔ the main receiver type is in ``ESCALATION_TYPES``, or a
    rendered ``routes[j]`` (same list as ``list_tenant_subroutes``) matches
    ``severity: critical`` and sends to an ``ESCALATION_TYPES`` receiver.

    When compliant, ``leaks`` (#2312) is exact only inside the tenant
    route's sub-tree and only for equality matchers: the tenant route's
    children are tried in render order (overrides, then routes), first
    match wins, and what no child takes stays on the main
    receiver. For each destination N whose receiver type is not in
    ``ESCALATION_TYPES`` — every listed sub-route, then the main receiver
    with an empty match — let ``C_N = match(N) ∪ {severity: critical}``:

    * N's match has a ``severity`` other than ``critical`` → N never
      receives a critical alert;
    * some earlier sub-route P (escalating or not) has ``match(P) ⊆ C_N``
      → P takes every critical alert N could match, so N never sees one;
    * otherwise the alert labelled exactly ``C_N`` reaches N, and N is
      listed with ``caught`` = ``C_N``.

    With *tenant* given, every alert under the tenant route also carries
    ``tenant=<tenant>`` (the route's own matcher), so the subset test runs
    against ``C_N ∪ {tenant: <tenant>}`` — an earlier ``match: {tenant:
    <tenant>}`` takes everything — and a sub-route matching another tenant
    receives nothing. Without it that label is left unknown, which can only
    list a destination too many.

    Outside that model: platform routes rendered AHEAD of the tenant route
    (e.g. ``alertname="Watchdog"``, ``component=custom`` and the other
    ``continue: false`` platform routes) are not considered, so a sub-route
    whose match names their values is listed although it never receives an
    alert — this side can only over-report. And a listed sub-route the
    generator does not render (invalid receiver content) is still treated as
    present: it may be listed itself, and as an earlier P it may hide a
    later N (under-report).

    Whether the main receiver escalates does not change whether a sub-route
    is listed: a sub-route that catches critical alerts takes them away from
    whichever destination would otherwise have escalated them.

    Call it only for a tenant with a main receiver.
    """
    subroutes = list_tenant_subroutes(routing_config)
    main_type = _subroute_receiver_type(routing_config)
    matches = [_subroute_match(ref, routing_config)
               for ref, _m, _rc, _inh in subroutes]

    target = None
    for (ref, match_str, sub_rc, _inh), match in zip(subroutes, matches):
        if (match.get("severity") == "critical"
                and _subroute_receiver_type(sub_rc) in ESCALATION_TYPES):
            target = f"{ref} ({match_str})"
            break
    if target is None:
        if main_type not in ESCALATION_TYPES:
            return EscalationFindings(None, [])
        target = "the main receiver"

    destinations = [(ref, match_str, _subroute_receiver_type(sub_rc), match)
                    for (ref, match_str, sub_rc, _inh), match
                    in zip(subroutes, matches)]
    destinations.append(("the main receiver", "", main_type, {}))
    leaks = []
    for pos, (ref, match_str, rtype, match) in enumerate(destinations):
        if rtype in ESCALATION_TYPES:
            continue
        if match.get("severity", "critical") != "critical":
            continue
        if tenant is not None and match.get("tenant", tenant) != tenant:
            continue
        caught = {"severity": "critical", **match}
        labels = caught if tenant is None else {**caught, "tenant": tenant}
        if any(all(labels.get(k) == v for k, v in earlier.items())
               for earlier in matches[:pos]):
            continue
        leaks.append((ref, match_str, rtype,
                      ", ".join(f"{k}={v}" for k, v in caught.items())))
    return EscalationFindings(target, leaks)


def _check_critical_escalation(messages: list[str], fmt, policy_name: str,
                               tenant: str, routing_config: dict) -> None:
    """Append the ``require_critical_escalation`` findings for one tenant.

    Non-compliance goes through *fmt* (strict → blocking ERROR, else WARN).
    A non-escalation destination that can still receive a critical alert
    (``critical_escalation_findings().leaks``) is always a plain WARN — it
    never blocks: it is a plain ``str``, not a ``SkippedEntryWarning``
    (#2489), and the text still avoids the ``skipping`` word.
    """
    target, leaks = critical_escalation_findings(routing_config, tenant)
    escalation = sorted(ESCALATION_TYPES)
    if target is None:
        main_type = _subroute_receiver_type(routing_config)
        messages.append(fmt(
            f"domain_policy '{policy_name}', tenant '{tenant}': "
            f"require_critical_escalation is set but severity=critical alerts "
            f"do not reach a receiver of type {escalation} (main receiver "
            f"type '{main_type}', and no rendered routes entry matches "
            f"severity=critical with such a receiver)",
            "add `routes: - match: {severity: critical}` with a pagerduty "
            "receiver to the tenant's _routing or its routing profile, or "
            "switch the main receiver.type to pagerduty"))
        return
    for ref, match, rtype, caught in leaks:
        if match:
            messages.append(
                f"  WARN: domain_policy '{policy_name}', tenant '{tenant}' "
                f"{ref} ({match}): receiver type '{rtype}' catches alerts "
                f"with {caught} before any receiver of type {escalation} "
                f"does, so they never reach one")
        else:
            messages.append(
                f"  WARN: domain_policy '{policy_name}', tenant '{tenant}': "
                f"severity=critical alerts that no sub-route catches go to "
                f"the main receiver (type '{rtype}'), not a receiver of type "
                f"{escalation}")


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

    ``require_critical_escalation: true`` (#2244) is judged by
    ``critical_escalation_findings``; a compliant tenant's non-escalation
    destination that can still receive a critical alert (#2312) is a WARN
    in both modes and never blocks.

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
        # #2244: None / false mean "not constrained"; a non-bool value is
        # fail-open like a non-list list constraint — strict surfaces it.
        escalation = constraints.get("require_critical_escalation")
        require_escalation = escalation is True
        if (strict and escalation is not None
                and not isinstance(escalation, bool)):
            messages.append(_fmt(
                f"domain_policy '{policy_name}': constraint "
                f"'require_critical_escalation' must be a boolean, got "
                f"{type(escalation).__name__} {escalation!r} — the "
                f"constraint cannot be enforced",
                "set 'require_critical_escalation' to true or false "
                "(unquoted)"))

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
            # #2326 review F4: a `tenants:` entry that is not a scalar id (a
            # mapping, a list) is unhashable and crashed the run here with a
            # TypeError. It names no tenant, so it enforces nothing — strict
            # says so (fail loud), lenient skips it, as for the other
            # malformed shapes above.
            if not isinstance(tenant, str):
                if strict:
                    messages.append(_fmt(
                        f"domain_policy '{policy_name}': 'tenants' entry "
                        f"must be a tenant id, got {type(tenant).__name__} "
                        f"— the entry cannot be enforced",
                        "list each tenant id as a plain YAML scalar"))
                continue
            if tenant not in routing_configs:
                continue
            # #2243: a sub-route that renders its own AM receiver
            # (`_routing.overrides`, and since #2245 `routes`) is held to the same constraints as the
            # main route. #2252: read from the sub-route's EFFECTIVE values —
            # what it declares, plus what it inherits from the tenant's main
            # route (see list_tenant_subroutes()).
            tenant_rc = routing_configs[tenant]
            if (require_escalation and isinstance(tenant_rc, dict)
                    and tenant_rc.get("receiver")):
                _check_critical_escalation(
                    messages, _fmt, policy_name, tenant, tenant_rc)
            targets = [(f"tenant '{tenant}'", None, tenant_rc, frozenset())]
            targets.extend(
                (f"tenant '{tenant}' {ref} ({match})", ref, sub_rc, inherited)
                for ref, match, sub_rc, inherited
                in list_tenant_subroutes(tenant_rc))
            for subject, ref, rc, inherited in targets:
                whose = "the tenant's" if ref is None else f"{ref}'s"

                def _src(key: str, _inh: frozenset = inherited) -> str:
                    """Value-origin note for a key the sub-route inherits."""
                    return (" (inherited from the tenant's main route)"
                            if key in _inh else "")

                def _who(key: str, _inh: frozenset = inherited,
                         _ref: str | None = ref, _whose: str = whose) -> str:
                    """Whose value the fix hint points at, for *key*."""
                    if key in _inh:
                        return (f"the tenant's (which {_ref} inherits) or "
                                f"{_ref}'s own")
                    return _whose

                # Check receiver type constraints
                recv = rc.get("receiver", {})
                recv_type = recv.get("type", "") if isinstance(recv, dict) else ""
                if recv_type:
                    if forbidden_types and recv_type in forbidden_types:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"{subject}: receiver type '{recv_type}' "
                            f"is forbidden",
                            f"domain forbids {sorted(forbidden_types)}; switch "
                            f"{whose} receiver.type to a compliant type "
                            f"or amend the domain policy"))
                    if allowed_types and recv_type not in allowed_types:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"{subject}: receiver type '{recv_type}' "
                            f"not in allowed types {sorted(allowed_types)}",
                            f"switch {whose} receiver.type to one of "
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
                                    f"{subject}: repeat_interval "
                                    f"'{tenant_repeat}'{_src('repeat_interval')} is not a valid duration "
                                    f"— cannot check against max '{max_repeat}'",
                                    "use duration syntax such as '30m' or "
                                    "'1h30m'; negative values are not allowed"))
                            elif tenant_sec > max_sec:
                                messages.append(_fmt(
                                    f"domain_policy '{policy_name}', "
                                    f"{subject}: repeat_interval "
                                    f"'{tenant_repeat}'{_src('repeat_interval')} exceeds max "
                                    f"'{max_repeat}'",
                                    f"lower {_who('repeat_interval')} repeat_interval to "
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
                                f"{subject}: repeat_interval "
                                f"'{tenant_repeat}'{_src('repeat_interval')} exceeds max '{max_repeat}'",
                                f"lower {_who('repeat_interval')} repeat_interval to "
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
                                    f"{subject}: group_wait "
                                    f"'{tenant_gw}'{_src('group_wait')} is not a valid duration "
                                    f"— cannot check against minimum "
                                    f"'{min_group_wait}'",
                                    "use duration syntax such as '30s' or "
                                    "'1m30s'; negative values are not allowed"))
                            elif tenant_sec < min_sec:
                                messages.append(_fmt(
                                    f"domain_policy '{policy_name}', "
                                    f"{subject}: group_wait "
                                    f"'{tenant_gw}'{_src('group_wait')} below minimum "
                                    f"'{min_group_wait}'",
                                    f"raise {_who('group_wait')} group_wait to "
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
                                f"{subject}: group_wait "
                                f"'{tenant_gw}'{_src('group_wait')} below minimum '{min_group_wait}'",
                                f"raise {_who('group_wait')} group_wait to "
                                f"'{min_group_wait}' or more, or lower the "
                                f"policy's min_group_wait"))

                # Check enforce_group_by
                if enforce_group_by:
                    tenant_gb = rc.get("group_by", [])
                    if isinstance(tenant_gb, list):
                        # #2503: judge the list the generator renders — a
                        # non-string element is dropped there, and set() of
                        # an unhashable one (`{a: 1}`) used to crash here.
                        tenant_gb = group_by_problems(tenant_gb)[0]
                        missing = set(enforce_group_by) - set(tenant_gb)
                        if missing:
                            messages.append(_fmt(
                                f"domain_policy '{policy_name}', "
                                f"{subject}: group_by{_src('group_by')} missing required "
                                f"labels: {sorted(missing)}",
                                f"add {sorted(missing)} to {_who('group_by')} group_by "
                                f"(policy requires {sorted(enforce_group_by)})"))
                    elif strict:
                        messages.append(_fmt(
                            f"domain_policy '{policy_name}', "
                            f"{subject}: group_by must be a list, got "
                            f"{type(tenant_gb).__name__} — cannot check "
                            f"enforce_group_by",
                            f"define {whose} group_by as a YAML list of "
                            "label names"))

    return messages
