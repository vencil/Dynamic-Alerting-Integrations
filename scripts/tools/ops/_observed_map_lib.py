#!/usr/bin/env python3
"""_observed_map_lib.py — Shared SoT extractor for the threshold observed-map (#719).

The threshold-recommend tool must compute percentile recommendations from the
OBSERVED workload series that each threshold is actually compared against — i.e.
the recording rule on the left of ``<observed> > tenant:alert_threshold:<key>``
in the rule-pack alert rules. That recording rule is, by construction, in the
SAME unit/topology as the threshold (the alert compares them directly), so no
unit conversion is needed and P95(observed) is a directly-usable threshold value.

This module derives ``conf.d key -> observed recording-rule series`` from the
rule packs via a robust **containment check** (no PromQL AST parsing — see #709
catastrophic-backtracking lesson). It is shared by:

  - ``threshold_recommend --generate-observed-map`` (writes the committed map)
  - ``check_threshold_observed_map.py`` (CI drift-guard: map <-> rule packs)

Guardrails baked in (#719 / Gemini adversarial review):
  - R1 multi-candidate: a composite alert referencing >1 observed series ->
    ``needs_review`` (don't auto-pick), unless a clean ``<key>_critical`` sibling
    disambiguates it.
  - R2 direction: capture ``>`` / ``<``. Lower-bound (``<``) metrics get
    ``needs_review`` (P95-upper recommendation is wrong for them; #916).
  - R3 denylist: only ``tenant:`` / ``tenant_version:`` colon-delimited recording
    rules count as observed; ``tenant_metadata_info`` / ``user_state_filter`` /
    ``tenant:alert_threshold:*`` are excluded.
  - Scaling guard: if a numeric ``* N`` / ``/ N`` is applied to the observed
    operand inside the alert, the bare recording-rule query would be off by a
    factor -> ``needs_review``.
"""
from __future__ import annotations

import re
from typing import Any, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - environments without pyyaml
    yaml = None  # type: ignore

# A threshold reference: tenant:alert_threshold:<key> or
# tenant_version:alert_threshold:<key> (ADR-024 version-aware packs).
THRESHOLD_RE = re.compile(r"tenant(?:_version)?:alert_threshold:([A-Za-z0-9_]+)")
# An observed recording rule: tenant:NAME:AGG or tenant_version:NAME:AGG.
# The colon convention naturally excludes tenant_metadata_info / user_state_filter
# (underscore, no colon) — part of the R3 denylist.
OBSERVED_RE = re.compile(r"tenant(?:_version)?:[A-Za-z0-9_:]+")
# Numeric scaling adjacent to an operand (e.g. "* 100", "/ 1024", "* (100)").
# The metadata join "* on(tenant) group_left(...) tenant_metadata_info" is NOT
# numeric, so it is not matched here. The optional "(" covers parenthesised
# scalars like "* (100)".
SCALING_RE = re.compile(r"[*/]\s*\(?\s*[0-9]")


def _alert_rules(doc: dict) -> list[dict]:
    out: list[dict] = []
    for g in doc.get("groups", []) or []:
        for r in g.get("rules", []) or []:
            if isinstance(r, dict) and "alert" in r:
                out.append(r)
    return out


def _direction_before(expr: str, key: str) -> Optional[str]:
    """Comparison operator (``>``/``<``) in the window before the threshold token.

    Scans backwards from the ``tenant:alert_threshold:<key>`` occurrence; skips
    ``=`` so ``>=`` / ``<=`` resolve to ``>`` / ``<``. The ``unless ... == 1``
    maintenance filter appears AFTER the comparison, so it is not picked up.

    The token is matched with a trailing word-boundary so ``key="cpu"`` does NOT
    match the ``cpu_critical`` token (a bare ``str.find`` would land on the
    prefix-sharing sibling when both appear in one composite expr and read the
    wrong operator — latent across the 14 ``<key>``/``<key>_critical`` pairs).
    """
    pat = re.compile(
        r"tenant(?:_version)?:alert_threshold:" + re.escape(key) + r"(?![A-Za-z0-9_])"
    )
    m = pat.search(expr)
    if m is None:
        return None
    window = expr[max(0, m.start() - 100):m.start()]
    for ch in reversed(window):
        if ch in "<>":
            return ch
    return None


def extract_pack(path: str) -> dict[str, dict[str, Any]]:
    """Extract per-key observed candidates from one rule pack.

    Returns ``{key: {candidates:set, directions:set, scaled:bool, alerts:set}}``.
    """
    if yaml is None:
        raise RuntimeError("pyyaml required")
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    by_key: dict[str, dict[str, Any]] = {}
    for rule in _alert_rules(doc):
        expr = rule.get("expr", "") or ""
        keys = set(THRESHOLD_RE.findall(expr))
        if not keys:
            continue
        cands = {
            m for m in OBSERVED_RE.findall(expr)
            if "alert_threshold" not in m  # exclude tenant[_version]:alert_threshold:*
        }
        scaled = bool(SCALING_RE.search(expr))
        for key in keys:
            e = by_key.setdefault(
                key,
                {"candidates": set(), "directions": set(), "scaled": False, "alerts": set()},
            )
            e["candidates"].update(cands)
            d = _direction_before(expr, key)
            if d:
                e["directions"].add(d)
            e["scaled"] = e["scaled"] or scaled
            e["alerts"].add(rule.get("alert", "?"))
    return by_key


def _pack_name(path: str) -> str:
    return path.replace("\\", "/").split("/")[-1]


# Recommendation engine currently supports only single-dimension (tenant) scope.
SUPPORTED_SCOPES = {"tenant"}


def scope_of(series: str) -> str:
    """Aggregation scope = the recording-rule prefix (``tenant`` / ``tenant_version``).

    The prefix IS the topology: ``tenant:X:agg`` is aggregated ``by(tenant)``;
    ``tenant_version:X:agg`` is ``by(tenant, version)`` (ADR-024). Derived, never
    hand-set; the drift-guard cross-checks scope == observed_series prefix.
    """
    return series.split(":", 1)[0] if ":" in series else "?"


def _scope_of_many(series_iter) -> str:
    prefixes = {scope_of(s) for s in series_iter}
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return "mixed"


def build_map(pack_paths: list[str]) -> dict[str, Any]:
    """Build the observed-map (one entry per threshold key across all packs).

    Resolution order per key:
      1. single candidate, single ``>`` direction, not scaled -> clean.
      2. multi-candidate but a clean ``<key>_critical`` sibling pins the series
         (and that series is among this key's candidates) -> resolved clean.
      3. otherwise -> needs_review with candidates + reason.
    Lower-bound (``<``) keys are always needs_review (R2).
    """
    raw: dict[str, dict[str, Any]] = {}
    pack_of: dict[str, str] = {}
    for p in pack_paths:
        for key, e in extract_pack(p).items():
            if key in raw:
                # same key in two packs: merge (rare); flag later
                raw[key]["candidates"].update(e["candidates"])
                raw[key]["directions"].update(e["directions"])
                raw[key]["scaled"] = raw[key]["scaled"] or e["scaled"]
            else:
                raw[key] = e
                pack_of[key] = _pack_name(p)

    # First pass: identify clean single-candidate keys (for sibling disambiguation).
    clean_series: dict[str, str] = {}
    for key, e in raw.items():
        if len(e["candidates"]) == 1 and e["directions"] == {">"} and not e["scaled"]:
            clean_series[key] = next(iter(e["candidates"]))

    out: dict[str, Any] = {}
    for key in sorted(raw):
        e = raw[key]
        cands = sorted(e["candidates"])
        dirs = sorted(e["directions"])
        entry: dict[str, Any] = {"pack": pack_of.get(key, "?")}
        reasons: list[str] = []

        # scope = aggregation topology, derived from the observed-series prefix.
        scope = _scope_of_many(e["candidates"]) if e["candidates"] else "?"
        entry["scope"] = scope

        direction = dirs[0] if len(dirs) == 1 else None
        if direction:
            entry["direction"] = direction

        # Only auto-resolve a single observed_series for SUPPORTED (tenant) scope.
        # Unsupported-scope (e.g. tenant_version) alerts are often compound
        # (sentinels cross-reference multiple thresholds) so containment can't
        # reliably pick the operand — emit candidates honestly for #916.
        resolved: Optional[str] = None
        if scope not in SUPPORTED_SCOPES:
            resolved = None
        elif len(cands) == 1 and direction == ">" and not e["scaled"]:
            resolved = cands[0]
        elif direction == ">" and not e["scaled"]:
            # sibling disambiguation: <key>_critical clean series, if a candidate.
            sib = clean_series.get(f"{key}_critical")
            if sib and sib in cands:
                resolved = sib
                entry["resolved_via"] = f"{key}_critical sibling"

        if resolved:
            entry["observed_series"] = resolved
        else:
            entry["candidates"] = cands

        if scope not in SUPPORTED_SCOPES:
            reasons.append(
                f"unsupported recommendation scope '{scope}' — engine supports "
                "'tenant' granularity only (deferred #916)"
            )
        if e["scaled"]:
            reasons.append("alert applies numeric scaling to observed operand — verify exact query expression")
        if not dirs:
            reasons.append("could not determine comparison direction")
        elif len(dirs) > 1:
            reasons.append(f"ambiguous direction {dirs}")
        elif dirs == ["<"]:
            reasons.append("lower-bound (<) metric — P95-upper recommendation not applicable (#916)")
        if not resolved and len(cands) != 1:
            reasons.append(f"{len(cands)} observed candidates in composite alert — pick one")

        if reasons:
            entry["needs_review"] = True
            entry["reason"] = "; ".join(reasons)
        out[key] = entry
    return out


def all_threshold_keys(pack_paths: list[str]) -> set[str]:
    """Every conf.d threshold key referenced by any rule pack (for coverage)."""
    keys: set[str] = set()
    if yaml is None:
        return keys
    for p in pack_paths:
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        keys.update(THRESHOLD_RE.findall(text))
    return keys


# ---------------------------------------------------------------------------
# Shared paths / loading / known-deferred (used by recommend + drift-guard)
# ---------------------------------------------------------------------------
import os  # noqa: E402
import sys  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/tools/ops/ -> repo root is three levels up.
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))

DEFAULT_MAP_PATH = os.path.join(_THIS_DIR, "metric_observed_map.yaml")
DEFAULT_RULE_PACKS_DIR = os.path.join(_REPO_ROOT, "rule-packs")

# Keys deliberately NOT in the observed-map: their threshold comparison lives in
# a recording rule (`:core`), not a `- alert:`, so the alert-based extractor
# cannot reach them. Version-aware (ADR-024) recommendation — extraction AND
# per-version logic — is tracked as #916. The drift-guard treats these as
# INFO (known-deferred), NOT an error, so this bugfix can merge.
KNOWN_DEFERRED: dict[str, str] = {
    "container_cpu": "version-aware (ADR-024) — recording-rule sourced, #916",
    "container_cpu_throttle": "version-aware (ADR-024) — recording-rule sourced, #916/#944",
    "container_memory": "version-aware (ADR-024) — recording-rule sourced, #916",
}


def default_pack_paths() -> list[str]:
    """Sorted list of rule-pack YAML paths under the repo's rule-packs/ dir."""
    import glob
    return sorted(glob.glob(os.path.join(DEFAULT_RULE_PACKS_DIR, "rule-pack-*.yaml")))


_MAP_HEADER = (
    "# SoT map: conf.d threshold key -> observed recording-rule series (#719).\n"
    "# GENERATED from rule-packs/*.yaml alert rules via\n"
    "#   da-tools threshold-recommend --generate-observed-map\n"
    "# The drift-guard (check_threshold_observed_map.py) cross-checks this map vs\n"
    "# the rule packs. needs_review entries are SKIPPED by threshold-recommend\n"
    "# until a human resolves them (pick one observed_series / confirm direction).\n"
)


def write_observed_map(
    out_path: Optional[str] = None,
    pack_paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Generate the observed-map from rule packs and write it to ``out_path``.

    Returns a summary dict ``{path, total, clean, needs_review}``.
    """
    if yaml is None:
        raise RuntimeError("pyyaml required")
    # write_text_secure performs the os.chmod(0o600) that the repo SAST
    # convention (tests/shared/test_sast.py) requires for write-mode files.
    sys.path.insert(0, _THIS_DIR)
    sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
    from _lib_python import write_text_secure  # noqa: E402

    packs = pack_paths or default_pack_paths()
    out = out_path or DEFAULT_MAP_PATH
    keys = build_map(packs)
    doc = {
        "version": 1,
        "_generated_by": "threshold-recommend --generate-observed-map (#719)",
        "keys": keys,
    }
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    write_text_secure(out, _MAP_HEADER + body)
    nr = sum(1 for v in keys.values() if v.get("needs_review"))
    return {"path": out, "total": len(keys), "clean": len(keys) - nr, "needs_review": nr}


def load_observed_map(path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Load the committed observed-map; returns the ``keys`` mapping (or {})."""
    if yaml is None:
        raise RuntimeError("pyyaml required")
    p = path or DEFAULT_MAP_PATH
    if not os.path.isfile(p):
        return {}
    with open(p, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    return doc.get("keys", {}) or {}


def resolve_observed(entry: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """Return (observed_series, skip_reason) for a map entry.

    skip_reason is None iff the key is usable for recommendation: a single
    resolved ``observed_series``, supported ``scope``, and not flagged
    ``needs_review``. Re-derives the decision from typed fields so a hand-edited
    map (e.g. a human resolving a composite) stays safe without trusting the
    generated ``reason`` string.
    """
    if entry.get("needs_review"):
        return None, entry.get("reason", "needs_review")
    scope = entry.get("scope", "?")
    if scope not in SUPPORTED_SCOPES:
        return None, (
            f"unsupported scope '{scope}' — engine supports 'tenant' only "
            "(deferred #916)"
        )
    direction = entry.get("direction")
    if direction != ">":
        # Require an explicit upper-bound direction. A missing/ambiguous
        # direction (e.g. a hand-edited or half-resolved entry) must NOT slip
        # through to a recommendation — the percentile strategy is only valid
        # for ``observed > threshold`` alerts.
        if direction == "<":
            return None, "lower-bound (<) metric — not supported (#916)"
        return None, "missing or ambiguous comparison direction — manual review required"
    series = entry.get("observed_series")
    if not series:
        return None, "no resolved observed_series (candidates need manual pick)"
    return series, None


def alert_referenced_keys(pack_paths: list[str]) -> set[str]:
    """Threshold keys that are actually referenced inside an ``- alert:`` expr.

    A key may have a ``record: tenant:alert_threshold:<key>`` recording rule but
    never be used by any alert (an orphan threshold). Those can't be mapped by
    the alert-based extractor and are NOT a map bug — they're a rule-pack gap.
    """
    keys: set[str] = set()
    if yaml is None:
        return keys
    for p in pack_paths:
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        for rule in _alert_rules(doc):
            keys.update(THRESHOLD_RE.findall(rule.get("expr", "") or ""))
    return keys


def check_consistency(
    observed_map: dict[str, dict[str, Any]],
    pack_paths: list[str],
) -> dict[str, list[str]]:
    """Cross-check a committed map against freshly-extracted rule-pack truth.

    Returns a dict with keys:
      - errors: hard drift — a mapped (key, observed_series) pair NOT found
        together in any rule-pack alert, or scope inconsistent with the
        observed_series prefix. CI should FAIL on these.
      - infos: known-deferred keys (KNOWN_DEFERRED allowlist) present in rule
        packs but absent from the map — INFO only, never an error.
      - orphan_thresholds: keys with a recording rule but referenced by NO alert
        — a rule-pack gap, not a map bug. WARN (not error); surfaced for the
        rule-pack authors.
      - coverage_gaps: alert-referenced threshold keys absent from the map AND
        not known-deferred — genuine extractor gaps. WARN so coverage is visible.
    """
    errors: list[str] = []
    infos: list[str] = []
    orphans: list[str] = []
    gaps: list[str] = []

    fresh = build_map(pack_paths)

    for key, entry in observed_map.items():
        fresh_entry = fresh.get(key)
        # A key carried in the map but no longer extractable from any rule-pack
        # alert is stale — catch it regardless of whether it is a resolved entry
        # or a needs_review/candidates one (KNOWN_DEFERRED keys legitimately are
        # not alert-extracted, so they are exempt).
        if fresh_entry is None and key not in KNOWN_DEFERRED and (
            entry.get("observed_series") or entry.get("candidates") or entry.get("needs_review")
        ):
            errors.append(
                f"{key}: present in map but no longer found in any rule-pack "
                f"alert (stale map entry — removed from rule packs?)"
            )
            continue
        # scope must equal the observed_series / candidates prefix.
        series_list = []
        if entry.get("observed_series"):
            series_list = [entry["observed_series"]]
        elif entry.get("candidates"):
            series_list = list(entry["candidates"])
        if series_list:
            pref = _scope_of_many(series_list)
            if entry.get("scope") != pref:
                errors.append(
                    f"{key}: scope '{entry.get('scope')}' != observed prefix '{pref}'"
                )
        # resolved observed_series must be a real candidate in the fresh extract.
        series = entry.get("observed_series")
        if series:
            fresh_cands = set()
            if fresh_entry:
                if fresh_entry.get("observed_series"):
                    fresh_cands.add(fresh_entry["observed_series"])
                fresh_cands.update(fresh_entry.get("candidates", []))
            if series not in fresh_cands:
                errors.append(
                    f"{key}: observed_series '{series}' not found paired with "
                    f"this key in any rule-pack alert (stale map?)"
                )

    all_keys = all_threshold_keys(pack_paths)
    alert_keys = alert_referenced_keys(pack_paths)
    for key in sorted(all_keys - set(observed_map)):
        if key in KNOWN_DEFERRED:
            infos.append(f"{key}: known-deferred — {KNOWN_DEFERRED[key]}")
        elif key not in alert_keys:
            orphans.append(key)  # recording rule exists but no alert uses it
        else:
            gaps.append(key)
    return {
        "errors": errors,
        "infos": infos,
        "orphan_thresholds": orphans,
        "coverage_gaps": gaps,
    }
