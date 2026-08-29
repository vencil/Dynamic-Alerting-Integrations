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
import os
import re
import sys
from pathlib import Path
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

    ⚠️ The empty-list return survives for exactly two inputs, and both mean
    "the operator asked for no constraint", not "the tool could not tell":
    no ``--policy`` at all, and a well-formed policy whose ``allowed_domains``
    is an empty list.
    """
    if not policy_path:
        return []
    if not Path(policy_path).is_file():
        raise PolicyInputError(
            f"--policy: not a file: {policy_path}\n"
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
            f"--policy: {policy_path} is not valid UTF-8: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PolicyInputError(
            f"--policy: {policy_path} is not valid YAML: {exc}") from exc
    except OSError as exc:
        raise PolicyInputError(
            f"--policy: cannot read {policy_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyInputError(
            f"--policy: top level of {policy_path} is "
            f"{type(data).__name__}, expected a mapping with `allowed_domains:`")
    domains = data.get("allowed_domains", [])
    if not isinstance(domains, list):
        raise PolicyInputError(
            f"--policy: `allowed_domains` in {policy_path} is "
            f"{type(domains).__name__}, expected a list")
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
    blocking prefix — otherwise the --validate fatal predicates
    (generate_alertmanager_routes._validate_mode / validate_config) would
    misclassify an advisory as a blocking failure.
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
    warnings containing ``"skipping"`` or on ``ERROR:``-prefixed policy lines,
    and ``unknown key … not in defaults`` is neither. So a divergence never
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
