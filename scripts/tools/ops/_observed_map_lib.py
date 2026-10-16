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
  - R2 direction: capture ``>`` / ``<``. A lower-bound (``<``) key's compared
    series is recorded like any other (#3017); whether the recommender may use
    it is its ``recommendation_mode`` (#916), and the drift-guard fails a ``<``
    key that is not classified.
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

# Per-entry recommendation-mode field (#916 Item A output; Item B only reads it
# here so a future build_map can round-trip it through a merge preserve).
MODE_FIELD = "recommendation_mode"

# #916 Item A — lower-bound (``<``) classification (code-level dicts, mirroring
# KNOWN_DEFERRED). A ``<`` threshold is NOT a blanket needs_review anymore; it is
# routed into one of three states by these two allowlists (any ``<`` key in
# NEITHER dict gets no mode — the recommender skips it as unsupported — and the
# drift-guard fails it as unclassified, #3017):
#
#   NOT_APPLICABLE_KEYS   -> ``recommendation_mode: not-applicable`` (no percentile
#       recommendation is ever meaningful — an expected-value invariant or a
#       topology floor). The value is a human rationale surfaced in the reason.
#   LOWER_BOUND_RESOLVED  -> ``recommendation_mode: percentile-lower`` (a genuine
#       lower percentile floor the engine can tune). The value is the observed
#       series the ``<`` alert compares against; build_map validates it is among
#       the freshly-extracted candidates (and ``<`` / not scaled) before honoring
#       it, so after a rule-pack rename the mode is not applied and the
#       drift-guard fails, naming the stale pick (#3017).
NOT_APPLICABLE_KEYS: dict[str, str] = {
    "kafka_active_controllers": "期望值==1 不變量",
    "kafka_broker_count": "拓撲下限",
    "rabbitmq_consumers": "整數 count 下限",
}
LOWER_BOUND_RESOLVED: dict[str, str] = {
    "db2_bufferpool_hit_ratio": "tenant:db2_bufferpool_hit_ratio:min",
}

# Reason marker for a by-design not-applicable ``<`` entry. MUST NOT contain the
# ``lower-bound (<)`` substring (threshold_govern classifies the two apart by
# marker; a collision would mis-route N/A keys into the ungoverned ``<`` list).
# Pinned by test_observed_map_lib.TestStringSafety.FORBIDDEN.
_NOT_APPLICABLE_MARKER = "by-design not-applicable"


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

    The map records FACTS — the series each key's alert compares and the
    direction it compares in (#3017: backtest reads them, not only the
    recommender). Recommender policy lives in ``recommendation_mode`` alone and
    is applied by its consumer (``resolve_observed``); the drift-guard fails a
    ``<`` key whose classification is missing or stale.

    Resolution order per key:
      1. single candidate, single direction (``>`` or ``<``), not scaled,
         supported scope -> ``observed_series``.
      2. ``>`` multi-candidate but a clean ``<key>_critical`` sibling pins the
         series (and that series is among this key's candidates) -> resolved.
      3. ``<`` multi-candidate: the LOWER_BOUND_RESOLVED pick, if a candidate.
      4. otherwise -> needs_review with candidates + reason.
    ``<`` keys additionally get their #916 ``recommendation_mode`` from
    NOT_APPLICABLE_KEYS / LOWER_BOUND_RESOLVED.
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
        elif len(dirs) > 1:
            # typed field (#916 Item B): >1 comparison direction across alerts.
            # merge/drift logic keys off this instead of parsing reason text.
            entry["directions"] = dirs
        if e["scaled"]:
            # typed field (#916 Item B): numeric scaling detected in the alert
            # expr; consumed by check_consistency (scaled drift) + _revalidate.
            entry["scaled"] = True

        # #916 Item A: lower-bound (``<``) three-state classification. A ``<`` key
        # in NOT_APPLICABLE_KEYS / LOWER_BOUND_RESOLVED short-circuits here; any
        # other ``<`` key falls through to the generic needs_review path below.
        if dirs == ["<"] and scope in SUPPORTED_SCOPES:
            if key in NOT_APPLICABLE_KEYS:
                # The compared series is a fact like any other key's (#3017:
                # backtest needs it); "not-applicable" is only recommender
                # policy. observed_series XOR candidates, as everywhere, so the
                # drift-guard's stale/scope checks still bind. No needs_review;
                # fixed by-design reason.
                if len(cands) == 1 and not e["scaled"]:
                    entry["observed_series"] = cands[0]
                else:
                    entry["candidates"] = cands
                entry[MODE_FIELD] = "not-applicable"
                entry["reason"] = (
                    f"{_NOT_APPLICABLE_MARKER} — {NOT_APPLICABLE_KEYS[key]} (#916)"
                )
                out[key] = entry
                continue
            if (
                key in LOWER_BOUND_RESOLVED
                and LOWER_BOUND_RESOLVED[key] in e["candidates"]
                and not e["scaled"]
            ):
                # Symmetric with the ``>`` auto-resolve gate: a resolved series
                # must be an actual candidate AND unscaled (a ``* N`` would make
                # the bare series off by a factor). direction=='<' is guaranteed
                # by ``dirs == ["<"]``.
                entry["observed_series"] = LOWER_BOUND_RESOLVED[key]
                entry[MODE_FIELD] = "percentile-lower"
                out[key] = entry
                continue
            # else (``<`` not in either dict, or a resolved key whose series no
            # longer matches the packs) -> fall through: the fact is recorded
            # (single candidate) or needs_review (composite), with no mode —
            # the drift-guard fails an unclassified or stale ``<`` key.

        # Only auto-resolve a single observed_series for SUPPORTED (tenant) scope.
        # Unsupported-scope (e.g. tenant_version) alerts are often compound
        # (sentinels cross-reference multiple thresholds) so containment can't
        # reliably pick the operand — emit candidates honestly for #916.
        resolved: Optional[str] = None
        if scope not in SUPPORTED_SCOPES:
            resolved = None
        elif len(cands) == 1 and direction in (">", "<") and not e["scaled"]:
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
        elif dirs == ["<"] and not resolved:
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
import functools  # noqa: E402
import glob  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # repo layout: _lib_compat
from _lib_compat import find_project_root  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

DEFAULT_MAP_PATH = os.path.join(_THIS_DIR, "metric_observed_map.yaml")

_PACK_GLOB = "rule-pack-*.yaml"


class RulePacksNotFoundError(FileNotFoundError):
    """No rule pack is reachable from this module's location (#1501)."""


def _find_rule_packs_dir() -> Optional[str]:
    """The directory holding the rule packs in THIS layout, or None.

    ⛔ Found, not counted (#1501). This used to be ``_THIS_DIR`` + three
    ``".."`` — right in the repo (``scripts/tools/ops/``), and ``/`` in the
    image, where build.sh flattens every tool into ``/opt/da-tools/``. The
    glob under ``/rule-packs`` then matched nothing, ``build_map([])`` returned
    ``{}``, and ``--generate-observed-map`` merged that over the committed map
    as "every key DROPPED" — a silent wipe to zero entries.

    Flat first: build.sh ``REPO_DATA_FILES`` copies every pack that references
    ``alert_threshold:`` beside this module (the set ``build_map`` can extract
    from at all; ``tests/ops/test_threshold_alerts.py`` pins that roster). The
    repo branch reads ``rule-packs/`` under THIS tree's root
    (``find_project_root``, #2845) — same shape as
    ``_threshold_alerts.find_rule_pack_paths``.
    """
    if glob.glob(os.path.join(_THIS_DIR, _PACK_GLOB)):
        return _THIS_DIR
    root = find_project_root(_THIS_DIR)
    if root is not None and (root / "rule-packs").is_dir():
        return str(root / "rule-packs")
    return None


# None when no layout carries the packs — default_pack_paths() turns that into
# a loud RulePacksNotFoundError instead of an empty list.
DEFAULT_RULE_PACKS_DIR = _find_rule_packs_dir()

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
    """Sorted rule-pack YAML paths for this layout; raises when there are none.

    ⛔ Never returns ``[]``. An empty pack list is not "nothing to extract" —
    every caller would read it as "no key is referenced anywhere", and
    ``write_observed_map`` would then drop every committed entry (#1501).
    """
    if DEFAULT_RULE_PACKS_DIR is None:
        raise RulePacksNotFoundError(
            f"no rule packs reachable from {_THIS_DIR}: none beside this "
            f"module ({_PACK_GLOB}), and no rule-packs/ directory at the root "
            f"of the scripts/tools tree this module sits in"
        )
    paths = sorted(glob.glob(os.path.join(DEFAULT_RULE_PACKS_DIR, _PACK_GLOB)))
    if not paths:
        raise RulePacksNotFoundError(
            f"{DEFAULT_RULE_PACKS_DIR} holds no {_PACK_GLOB}"
        )
    return paths


_MAP_HEADER = (
    "# SoT map: conf.d threshold key -> observed recording-rule series (#719).\n"
    "# GENERATED from rule-packs/*.yaml alert rules via\n"
    "#   da-tools threshold-recommend --generate-observed-map\n"
    "# The drift-guard (check_threshold_observed_map.py) cross-checks this map vs\n"
    "# the rule packs. needs_review entries are SKIPPED by threshold-recommend\n"
    "# until a human resolves them (pick one observed_series / confirm direction).\n"
    "#\n"
    "# MERGE SEMANTICS (#916 Item B): regeneration MERGES over the committed map\n"
    "# rather than clobbering it, so human-resolved picks survive rule-pack edits.\n"
    "# Three states per key on regenerate:\n"
    "#   - DROP     : the key is no longer alert-extractable -> removed (WARN).\n"
    "#   - PRESERVE : a still-valid manual pick is kept when the generator can no\n"
    "#                longer determine the series on its own (WARN: verify pick).\n"
    "#   - DEMOTE   : a manual pick that no longer revalidates falls back to the\n"
    "#                fresh needs_review entry, annotated with why (WARN).\n"
    "# A generator-determinate series always wins (fresh-wins); a generated entry\n"
    "# that goes ambiguous simply falls back to needs_review (fail-safe).\n"
    "#\n"
    "# HAND-EDITS: free-form YAML comments do NOT survive regeneration. Put the\n"
    "# rationale for a manual pick in a per-entry `refs:` list (it is carried\n"
    "# across merges). To resolve a needs_review entry by hand, KEEP its\n"
    "# `candidates:` list, add `observed_series:` with the chosen series, add\n"
    "# `resolved_via: manual`, and delete `needs_review:`. That exact shape is\n"
    "# what the merge recognizes as a human pick to preserve.\n"
)


def _is_manual(old_e: dict[str, Any]) -> bool:
    """Human-pick fingerprint (narrowed — inspects the OLD entry only).

    Any one of three independent shapes marks an entry as human-resolved:
      1. explicit ``resolved_via: manual`` (the codified resolve step),
      2. a human ``refs`` list (rationale the generator never emits),
      3. the structural shape of a needs_review entry a human resolved by
         filling ``observed_series`` WITHOUT deleting ``candidates`` — a shape
         no generated entry has (build_map emits candidates XOR observed_series).

    No generated entry scores here: build_map emits observed_series XOR
    candidates (since #3017 a single-candidate not-applicable entry too), and
    never ``refs`` or ``resolved_via: manual``. (Pinned by
    test_idempotent_pin_on_committed_map: a misfire would PRESERVE and warn.)

    Note ``resolved_via`` values other than "manual" (e.g. the generated
    ``<key>_critical sibling``) are NOT manual — only the literal "manual".
    """
    return (
        old_e.get("resolved_via") == "manual"
        or bool(old_e.get("refs"))
        or bool(old_e.get("candidates") and old_e.get("observed_series"))
    )


def _revalidate(old_e: dict[str, Any], fresh_e: dict[str, Any]) -> tuple[bool, str]:
    """Re-check a manual pick against fresh rule-pack truth. ALL gates must pass.

    Returns ``(ok, why)``; ``why`` is empty on success, else a stable reason
    phrase. Compares against the entry's OWN declared direction (not a hardwired
    ``>``) so a future Item A ``<``-resolved entry can revalidate. None of the
    ``why`` phrases contain the threshold_govern marker substrings (see the
    string-safety test).

    NOTE: this intentionally does NOT gate on ``SUPPORTED_SCOPES``. Preserving an
    unsupported-scope manual pick is harmless — ``resolve_observed`` returns None
    (skip) for any unsupported scope downstream, so it can never reach a
    recommendation. Gating here would instead DEMOTE such a pick on every regen
    and churn the map; letting it survive keeps the human's work intact.
    """
    series = old_e.get("observed_series")
    if not series:
        return False, "no observed_series on manual entry"
    # legal set = fresh observed_series UNION fresh candidates (aligns with the
    # union semantics check_consistency already uses for fresh_cands).
    legal = set(fresh_e.get("candidates", []) or [])
    if fresh_e.get("observed_series"):
        legal.add(fresh_e["observed_series"])
    if series not in legal:
        return False, "pick no longer a candidate"
    if "direction" not in fresh_e:
        return False, "comparison direction ambiguous/undetermined in rule packs"
    if fresh_e["direction"] != old_e.get("direction"):
        return False, "direction changed"
    if fresh_e.get("scaled"):
        return False, "alert now applies numeric scaling"
    if old_e.get("scope") != fresh_e.get("scope"):
        return False, "scope changed"
    return True, ""


def merge_maps(
    old: dict[str, dict[str, Any]],
    fresh: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], list[str], dict[str, int]]:
    """Merge freshly-extracted truth over a committed map, preserving human work.

    Returns ``(merged, warns, stats)`` where ``stats`` counts
    ``{preserved, demoted, dropped, overridden}``. See ``_MAP_HEADER`` for the
    three-state contract. Fail-safe by construction: a generated entry that goes
    ambiguous falls back to the fresh needs_review form (never silently kept);
    manual picks are only preserved when they still revalidate, and are rebuilt
    from an ALLOWLIST of fresh fields (never a blind ``{**old_e}`` merge).
    """
    warns: list[str] = []
    stats = {"preserved": 0, "demoted": 0, "dropped": 0, "overridden": 0}
    merged: dict[str, Any] = {}

    # 態1 DROP: keys carried in old but no longer extractable from fresh.
    for key in old:
        if key not in fresh:
            warns.append(f"{key}: no longer extractable from rule packs — dropped")
            stats["dropped"] += 1

    for key, fresh_e in fresh.items():
        # Shallow copy is enough: each fresh_e is consumed exactly once here and
        # then discarded, so an aliased candidates list can never be mutated by a
        # later iteration. (We never write through base into fresh_e's nested
        # lists — only add/replace top-level keys.)
        base = dict(fresh_e)
        old_e = old.get(key)
        if old_e is None:
            merged[key] = base
            continue

        carry_refs = True
        if fresh_e.get("observed_series"):
            # Generator is determinate -> fresh wins.
            merged[key] = base
            if (
                _is_manual(old_e)
                and old_e.get("observed_series")
                and old_e.get("observed_series") != fresh_e.get("observed_series")
            ):
                warns.append(
                    f"{key}: manual pick '{old_e['observed_series']}' overridden by "
                    f"generator-determinate resolution '{fresh_e['observed_series']}'"
                )
                stats["overridden"] += 1
                # Override: the old manual value AND its rationale are superseded
                # by the generator. Do NOT carry the old refs onto the new pick —
                # they describe the now-rejected pick and would mislead. The
                # overridden WARN is sufficient provenance.
                carry_refs = False
        elif not _is_manual(old_e):
            # Fresh non-determinate + old is a generated entry -> fail-safe:
            # let it fall back to needs_review. Do NOT preserve, do NOT overwrite.
            merged[key] = base
        else:
            # Fresh non-determinate + old is a manual pick -> revalidate.
            ok, why = _revalidate(old_e, fresh_e)
            if ok:
                # 態3 PRESERVE — ALLOWLIST rebuild (never {**old_e}); do not carry
                # old reason/needs_review/candidates.
                rebuilt: dict[str, Any] = {
                    "pack": fresh_e["pack"],
                    "scope": fresh_e["scope"],
                    "direction": fresh_e["direction"],
                    "observed_series": old_e["observed_series"],
                    "resolved_via": "manual",
                }
                # The #916 classification is policy the generator owns (the
                # allowlists): take the fresh one, else the drift-guard's
                # "regenerate" hint could never clear a missing mode (#3017).
                if MODE_FIELD in fresh_e:
                    rebuilt[MODE_FIELD] = fresh_e[MODE_FIELD]
                elif MODE_FIELD in old_e:
                    rebuilt[MODE_FIELD] = old_e[MODE_FIELD]
                merged[key] = rebuilt
                warns.append(
                    f"{key}: previously-resolved entry preserved across a rule-pack "
                    f"change — verify the pick is still correct"
                )
                stats["preserved"] += 1
            else:
                # 態2 DEMOTE — fall back to fresh, annotate reason (.get, never +=).
                merged[key] = base
                base["reason"] = base.get("reason", "") + f"; manual resolution invalidated: {why}"
                warns.append(
                    f"{key}: manual resolution invalidated ({why}) — demoted to needs_review"
                )
                stats["demoted"] += 1

        # refs unified tail (preserve/demote/fresh-wins-same paths): carry a human
        # refs list when fresh lacks one. Skipped on override (carry_refs=False,
        # see above). Never write None/null.
        if carry_refs and old_e and old_e.get("refs") and not merged[key].get("refs"):
            merged[key]["refs"] = list(old_e["refs"])

    return merged, warns, stats


def write_observed_map(
    out_path: Optional[str] = None,
    pack_paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Generate the observed-map from rule packs and write it to ``out_path``.

    Regeneration MERGES over an existing committed map (merge_maps) so a human's
    resolved picks survive rule-pack edits; a first-time write (no file yet) uses
    the fresh extract directly. Returns a summary dict
    ``{path, total, clean, needs_review, preserved, demoted, dropped}``.
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
    fresh = build_map(packs)
    try:
        old = load_observed_map(out)
    except ObservedMapError as exc:
        # Nothing to preserve in a missing or key-less map: build it fresh.
        # An unreadable one may hold human picks — refuse, never overwrite.
        if exc.kind == "unreadable":
            raise
        old = None
    if old is not None:
        keys, warns, stats = merge_maps(old, fresh)
        for msg in warns:
            print(f"[WARN] {msg}", file=sys.stderr)
    else:
        keys = fresh
        stats = {"preserved": 0, "demoted": 0, "dropped": 0, "overridden": 0}
    doc = {
        "version": 1,
        "_generated_by": "threshold-recommend --generate-observed-map (#719)",
        "keys": keys,
    }
    body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False)
    write_text_secure(out, _MAP_HEADER + body)
    nr = sum(1 for v in keys.values() if v.get("needs_review"))
    return {
        "path": out,
        "total": len(keys),
        "clean": len(keys) - nr,
        "needs_review": nr,
        "preserved": stats["preserved"],
        "demoted": stats["demoted"],
        "dropped": stats["dropped"],
    }


class ObservedMapError(Exception):
    """The observed-map cannot be used. ``kind`` is ``"missing"``,
    ``"unreadable"`` (cannot be read or parsed) or ``"empty"`` (no non-empty
    ``keys`` mapping); the generator rebuilds over a missing or empty map but
    never over an unreadable one, which may still hold human picks."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def load_observed_map(path: Optional[str] = None) -> dict[str, dict[str, Any]]:
    """Load the committed observed-map's ``keys`` mapping.

    ⛔ Fails closed (#3017): raises ``ObservedMapError`` when the file is
    absent, cannot be read or parsed, or holds no non-empty ``keys`` mapping.
    It used to return ``{}`` there, and every consumer read that as "no key is
    mapped": threshold-recommend skipped every key at rc 0 and the drift-guard
    passed with every alert key a WARN-only gap — a packaging or path slip
    looked like a clean run (the #1501 shape: "found nothing" is not "nothing
    to find"). The generator itself only loads a map that exists.
    """
    if yaml is None:
        raise RuntimeError("pyyaml required")
    p = path or DEFAULT_MAP_PATH
    regen = "regenerate with `threshold-recommend --generate-observed-map`"
    if not os.path.isfile(p):
        raise ObservedMapError(f"{p}: observed-map not found ({regen})", "missing")
    try:
        with open(p, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # One line: a YAMLError's str() spans several (problem + context mark).
        why = " ".join(str(exc).split())
        raise ObservedMapError(
            f"{p}: cannot read observed-map ({why}); fix or delete it, then "
            "regenerate", "unreadable") from exc
    if doc is None:
        doc = {}  # an empty file: nothing in it, same as no keys
    if not isinstance(doc, dict) or not isinstance(doc.get("keys"), (dict, type(None))):
        raise ObservedMapError(
            f"{p}: observed-map is not a mapping with a 'keys' mapping; fix or "
            "delete it, then regenerate", "unreadable")
    keys = doc.get("keys")
    if not keys:
        raise ObservedMapError(
            f"{p}: observed-map has no keys ({regen})", "empty")
    return keys


def exit_on_observed_map_error(fn):
    """Decorate a CLI ``main`` so an unusable observed-map (``ObservedMapError``)
    exits 2 (``EXIT_CALLER_ERROR``, an environment error) with one ``ERROR:``
    line naming the file, instead of a traceback at rc 1."""
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ObservedMapError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
    return _wrapped


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
    if direction == "<":
        # #916 Item A: a lower-bound key is usable ONLY when it was classified
        # ``percentile-lower`` with a resolved series (analyze_tenant then routes
        # it to recommend_threshold_lower); ``not-applicable`` skips with the
        # by-design marker; anything else (a bare/half-resolved ``<`` entry) is
        # the fail-loud not-supported skip.
        mode = entry.get(MODE_FIELD)
        series = entry.get("observed_series")
        if mode == "percentile-lower" and series:
            return series, None
        if mode == "not-applicable":
            return None, f"{_NOT_APPLICABLE_MARKER} (#916) — no percentile recommendation"
        return None, "lower-bound (<) metric — not supported (#916)"
    if direction != ">":
        # Require an explicit upper-bound direction. A missing/ambiguous
        # direction (e.g. a hand-edited or half-resolved entry) must NOT slip
        # through to a recommendation — the percentile strategy is only valid
        # for ``observed > threshold`` alerts.
        return None, "missing or ambiguous comparison direction — manual review required"
    series = entry.get("observed_series")
    if not series:
        return None, "no resolved observed_series (candidates need manual pick)"
    return series, None


# The recommender's usability policy for a map entry (#3017: named apart from
# the backtest's, which reads the same facts).
usable_for_recommend = resolve_observed

_BACKTEST_DIRECTIONS = {">": "above", "<": "below"}


def lookup(observed_map: dict[str, dict[str, Any]], key: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """``(entry, None)`` for a threshold key in canonical spelling (a caller
    canonicalizes retired aliases and drops a ``{...}`` dimension part), or
    ``(None, reason)`` when the map has no entry: a KNOWN_DEFERRED key, whose
    alerts compare a version-aware recording rule the map does not record, or
    a key no shipped alert compares. A ``<base>_critical`` key is its own entry
    (a pack that normalizes only severity="warning" has none for it)."""
    entry = observed_map.get(key)
    if entry is not None:
        return entry, None
    if key in KNOWN_DEFERRED:
        return None, ("its alerts compare a version-aware recording rule the "
                      f"observed-map does not record (KNOWN_DEFERRED: {KNOWN_DEFERRED[key]})")
    return None, "no shipped alert compares this key (not in metric_observed_map.yaml)"


def usable_for_backtest(entry: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(observed_series, "above" | "below", None)`` for an entry a backtest
    can replay — the series the alert compares and the way it compares — or
    ``(None, None, reason)``. Unlike ``usable_for_recommend`` this ignores
    ``recommendation_mode``: a by-design not-applicable floor still fires, and
    replaying it is exactly what a threshold change needs.

    ⚠️ The series is the alert's comparison operand, not the alert: a key
    resolved via its ``_critical`` sibling (``resolved_via``) or by hand may
    also feed composite AND alerts, and ``for:`` / maintenance filters are not
    in the map — a replay of the operand approximates when the alert fires.
    A scaled alert (``scaled``) never carries ``observed_series``."""
    if entry.get("needs_review"):
        return None, None, f"observed-map entry needs review: {entry.get('reason', 'needs_review')}"
    scope = entry.get("scope", "?")
    if scope not in SUPPORTED_SCOPES:
        return None, None, f"unsupported scope '{scope}' — only per-tenant series are queried"
    direction = _BACKTEST_DIRECTIONS.get(entry.get("direction"))
    if direction is None:
        return None, None, "alert comparison direction is not resolved in the observed-map"
    series = entry.get("observed_series")
    if not series:
        return None, None, "no single observed series (composite alert) in the observed-map"
    return series, direction, None


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
    enforce_known_deferred: bool = False,
) -> dict[str, list[str]]:
    """Cross-check a committed map against freshly-extracted rule-pack truth.

    Returns a dict with keys:
      - errors: hard drift — a mapped (key, observed_series) pair NOT found
        together in any rule-pack alert, scope inconsistent with the
        observed_series prefix, comparison-direction drift, the alert now
        scaling the observed operand, or (when ``enforce_known_deferred``) a
        KNOWN_DEFERRED exit-lock violation. CI should FAIL on these.
      - infos: known-deferred keys (KNOWN_DEFERRED allowlist) present in rule
        packs but absent from the map — INFO only, never an error.
      - orphan_thresholds: keys with a recording rule but referenced by NO alert
        — a rule-pack gap, not a map bug. WARN (not error); surfaced for the
        rule-pack authors.
      - coverage_gaps: alert-referenced threshold keys absent from the map AND
        not known-deferred — genuine extractor gaps. WARN so coverage is visible.

    ``enforce_known_deferred`` defaults to False so hermetic tests that feed a
    synthetic pack without the container_* keys are unaffected. The real-map
    lint path (check_threshold_observed_map.py) passes True to lock the
    KNOWN_DEFERRED allowlist against silent drift.
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
        # direction drift (#916 Item B): the alert's comparison operator changed
        # out from under a mapped direction. fresh_entry is None -> handled by the
        # stale path above, so only check when a fresh entry exists.
        if entry.get("direction") and fresh_entry:
            if "direction" in fresh_entry:
                if fresh_entry["direction"] != entry["direction"]:
                    errors.append(
                        f"{key}: direction drift — map '{entry['direction']}' vs "
                        f"rule-pack '{fresh_entry['direction']}' (alert comparison "
                        f"changed; regenerate)"
                    )
            else:
                errors.append(
                    f"{key}: comparison direction became ambiguous/undetermined in "
                    f"rule packs — regenerate"
                )
        # scaled drift (#916 Item B): the alert now numerically scales the observed
        # operand, so the bare mapped series is off by a factor.
        if entry.get("observed_series") and fresh_entry and fresh_entry.get("scaled"):
            errors.append(
                f"{key}: alert now numerically scales the observed operand — the "
                f"mapped series is off by a factor; regenerate"
            )
        # recommendation_mode authority closure (#916 Item A): a mode is only
        # legal for a key in the corresponding code-level allowlist. A hand-edited
        # map that stamps ``percentile-lower`` onto an integer-count key (to make
        # it flow into the lower engine and produce garbage) is a hard error.
        mode = entry.get(MODE_FIELD)
        if mode == "percentile-lower" and key not in LOWER_BOUND_RESOLVED:
            errors.append(
                f"{key}: recommendation_mode 'percentile-lower' but key not in "
                f"LOWER_BOUND_RESOLVED (_observed_map_lib.py) — mode spoofed; "
                f"remove the mode or add the key to the allowlist"
            )
        if mode == "not-applicable" and key not in NOT_APPLICABLE_KEYS:
            errors.append(
                f"{key}: recommendation_mode 'not-applicable' but key not in "
                f"NOT_APPLICABLE_KEYS (_observed_map_lib.py) — mode spoofed; "
                f"remove the mode or add the key to the allowlist"
            )
        # #3017: the map records the compared series as a fact for every key,
        # so a '<' key's #916 classification is no longer implied by
        # needs_review — it must be applied, or the recommender quietly
        # treats the key as unsupported.
        # Composite or scaled '<' entries (needs_review) are classified too:
        # the fail-loud must not vanish exactly when the key is ambiguous.
        if entry.get("direction") == "<":
            if key in LOWER_BOUND_RESOLVED and mode != "percentile-lower":
                pick = LOWER_BOUND_RESOLVED[key]
                compared = entry.get("observed_series") or entry.get("candidates")
                if entry.get("observed_series") == pick:
                    errors.append(
                        f"{key}: in LOWER_BOUND_RESOLVED but recommendation_mode is "
                        f"{mode!r} — regenerate the observed-map"
                    )
                else:
                    errors.append(
                        f"{key}: LOWER_BOUND_RESOLVED pick '{pick}' is not the series "
                        f"its alert compares ({compared!r}) — update the allowlist "
                        f"(_observed_map_lib.py); did a rule pack rename it?"
                    )
            elif key in NOT_APPLICABLE_KEYS and mode != "not-applicable":
                errors.append(
                    f"{key}: in NOT_APPLICABLE_KEYS but recommendation_mode is "
                    f"{mode!r} — regenerate the observed-map"
                )
            elif key not in LOWER_BOUND_RESOLVED and key not in NOT_APPLICABLE_KEYS:
                errors.append(
                    f"{key}: alert compares with '<' but the key is not classified "
                    f"— add it to NOT_APPLICABLE_KEYS or LOWER_BOUND_RESOLVED "
                    f"(_observed_map_lib.py, #916)"
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

    # KNOWN_DEFERRED exit-lock (#916 Item B): the allowlist is a promise that
    # these keys are recording-rule-sourced and hand-managed. Enforce that the
    # promise still holds so a silently-changed rule pack can't leave a stale
    # deferral. Message names the symbol, not a line number (refactor-proof).
    if enforce_known_deferred:
        for k in KNOWN_DEFERRED:
            if k in fresh:
                errors.append(
                    f"{k}: KNOWN_DEFERRED (_observed_map_lib.py) key is now "
                    f"alert-extractable — regenerate the map and remove it from "
                    f"KNOWN_DEFERRED"
                )
            if k not in all_keys:
                errors.append(
                    f"{k}: KNOWN_DEFERRED (_observed_map_lib.py) key is gone from "
                    f"the rule packs — remove it from KNOWN_DEFERRED"
                )
            if k in observed_map:
                errors.append(
                    f"{k}: KNOWN_DEFERRED (_observed_map_lib.py) key was hand-added "
                    f"to the map — deferred keys are not supported in the map; "
                    f"remove the entry"
                )

    return {
        "errors": errors,
        "infos": infos,
        "orphan_thresholds": orphans,
        "coverage_gaps": gaps,
    }
