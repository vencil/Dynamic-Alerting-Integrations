#!/usr/bin/env python3
"""Blast Radius diff engine — compare base vs PR effective tenant configs.

Usage:
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json --format markdown
    python3 scripts/tools/ops/blast_radius.py --base base.json --pr pr.json --output report.json

Consumes two JSON files produced by `describe-tenant --all --output`.
Diffs per-tenant effective configs and classifies changes into tiers:
  - Tier A: threshold / routing receiver changes (highlight)
  - Tier B: other alerting field changes (list)
  - Tier C: PROVABLY format-only changes (count only)
⚠️ Tier C is not the fallback. Anything unclassified is Tier B, because
Tier C renders as "no threshold/routing/alerting impact" — a positive
claim of harmlessness that has to be earned (#1419).

Outputs structured JSON report or PR comment markdown.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from _lib_python import format_json_report  # noqa: E402

# ---------------------------------------------------------------------------
# Tier classification constants
# ---------------------------------------------------------------------------

# Tier A: high-impact fields (threshold values, routing receivers)
TIER_A_PATTERNS = (
    "alerts.threshold",
    "alerts.thresholds",
    "_routing.receiver",
    "_routing.receivers",
    "receivers",
    # Custom alert recipes (ADR-024 Capability B, #741). A tenant-authored
    # declarative custom alert is a real, high-impact alerting change: it
    # adds / removes / retunes a paging rule carrying its own threshold. It
    # must be highlighted, never treated as format-only. flatten_dict keeps the
    # list as one opaque value, so the field path is exactly "_custom_alerts"
    # (the "alerts" / "_alerting" Tier-B patterns do NOT substring-match it).
    "_custom_alerts",
    # ⚠️ Explicit, because the segment-boundary matcher no longer reaches it
    # via the "_custom_alerts" prefix. Losing it was an UNINTENDED side effect
    # of that fix: it carries each recipe's provenance — which `_defaults` a
    # paging rule was inherited from — so a change here can move a pager
    # between owners. That belongs in the highlight tier, and demoting it
    # should be a decision rather than a by-product.
    # ⛔ NO key count in this comment, and none is coming back. Versions of that
    # one number have been wrong in several distinct ways: arithmetically right
    # but with its scope never stated (a union over every conf.d tree in the
    # repo); quoting "the repo's two conf.d trees" when there are many more than
    # two; a test comment disagreeing with the helper beside it; and a figure
    # retracted as unreproducible when it was reproducible under the scope
    # nobody had written down.
    # ⚠️ An earlier draft of this very paragraph made the point by listing those
    # numbers out — a "no counts here" rule with counts under it, some of them
    # current-state figures that drift and that nothing asserts. The failure
    # modes are the lesson; the digits were the thing being warned about. A
    # count of this kind carries a scope, prose keeps dropping the scope, and
    # the number then reads as measured.
    # `test_tier_membership_is_exhaustive_over_the_real_pipeline`
    # re-derives the A→B set from the live producer, and names its own scope in
    # code where it cannot drift away from the number.
    "_custom_alerts_resolution",
)

# The two fields that carry a tenant's custom-alert recipe list. They are
# written by the same producer loop from the same `resolved` list, so anything
# that treats one of them specially has to name both or it is treating a half.
RECIPE_LIST_FIELDS = ("_custom_alerts", "_custom_alerts_resolution")

# Tier B: other alerting fields (non-threshold, non-receiver)
TIER_B_PATTERNS = (
    "alerts",
    "_routing",
    "_alerting",
    "rules",
    "rule_groups",
    "severity",
    "inhibit_rules",
    "notification",
    "escalation",
)

# Top-level keys of the PLATFORM DEFAULTS document that are structural rather
# than thresholds. ⛔ Needed because "a bare key inside effective_config is a
# threshold" is NOT true by construction, which an earlier version of the
# comment below claimed: `describe_tenant` merges `ddata.get("defaults", ddata)`,
# so a `_defaults.yaml` with no `defaults:` block merges its WHOLE document —
# and `docs/schemas/platform-defaults.schema.json` has no top-level `required`,
# so that is legal. ⚠️ An earlier version of this comment cited the repo's own
# shipped `_defaults.yaml` as the motivating case — unreachable: that file HAS
# a `defaults:` wrapper, so only its `defaults:` block is merged and
# `optional_overrides` reaches no tenant's effective_config (measured: 0 of 5).
# The real exposure is any wrapper-less file, which the schema permits and the
# repo already contains two of.
#
# Mirrored from that schema's non-`_` top-level properties minus `defaults`;
# `tests/ops/test_blast_radius.py` re-derives the set from the schema and fails
# on drift, so this is a cache of an authoritative definition, not a hand list.
PLATFORM_DOCUMENT_KEYS = frozenset({
    "max_metrics_per_tenant",
    "optional_overrides",
    "profiles",
    "state_filters",
    "tenants",
})

# Fields that are metadata / non-semantic (changes here → Tier C)
METADATA_FIELDS = frozenset({
    "_metadata",
    "_comment",
    "_comments",
    "_description",
})


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def flatten_dict(d: dict, prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict to dot-separated key paths.

    Example: {"a": {"b": 1}} → {"a.b": 1}
    """
    items: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.update(flatten_dict(v, key))
        else:
            items[key] = v
    return items


def _matches_field(field_path: str, pattern: str) -> bool:
    """Does *pattern* name whole field(s) inside *field_path*?

    ⛔ Segment-aware, because the patterns name FIELDS. The previous test was
    `startswith(pattern) or f".{pattern}" in ...`, i.e. a bare prefix match, so
    any metric key that merely began with a Tier B word was silently demoted out
    of Tier A before the bare-key rule below could see it. Measured on the
    shipped patterns: `rules_engine_lag_seconds`, `severity_score_p99`,
    `alerts_dropped_total` and `notification_queue_depth` all classified as B —
    every one of them a legal tenant threshold key (`tenant-config.schema.json`
    puts no name restriction on `additionalProperties`), so halving any of them
    rendered as "other alerting" rather than the highlight tier (#1419).
    """
    return f".{pattern}." in f".{field_path}."


def classify_field(field_path: str) -> str:
    """Classify a dot-separated field path into Tier A, B, or C.

    Returns: "A", "B", or "C"
    """
    # Check metadata fields first (always Tier C)
    top_key = field_path.split(".")[0]
    if top_key in METADATA_FIELDS:
        return "C"

    # Check Tier A patterns (most specific first)
    for pattern in TIER_A_PATTERNS:
        if _matches_field(field_path, pattern):
            return "A"

    # Check Tier B patterns
    for pattern in TIER_B_PATTERNS:
        if _matches_field(field_path, pattern):
            return "B"

    # ⛔ A bare key inside effective_config is a threshold UNLESS it is one of
    # the platform document's own structural keys (see PLATFORM_DOCUMENT_KEYS).
    # This is what `diff_configs` is actually fed — `compute_blast_radius`
    # passes `base_info["effective_config"]`, whose bare keys are metric keys
    # (`mysql_connections`, `es_heap_usage_percent{node="es-hot-01"}`,
    # `<base>_critical`, …). None of those matched a Tier A pattern, so every
    # real threshold change — including halving a limit or switching one to
    # "disable" — was reported as "format-only … no threshold/routing/alerting
    # impact" (#1419).
    # ⚠️ An earlier version of this comment said the key space was "exactly
    # `_`-prefixed structural keys vs metric keys". Two reviewers falsified it
    # independently: the wrapper-less `_defaults.yaml` path merges the platform
    # document itself, which is why PLATFORM_DOCUMENT_KEYS exists.
    # ⚠️ A later version then claimed "the schema allows additionalProperties,
    # so a new bare structural key would be read as a threshold" — BACKWARDS.
    # `platform-defaults.schema.json` sets `additionalProperties: false` and its
    # only `patternProperties` are `^_state_` / `^_routing`, both underscore-led,
    # so the document cannot grow a bare structural key without a schema change
    # — which the drift test below would catch.
    # ⚠️ The Tier A patterns above were written against `alerts.threshold.*`,
    # a shape describe-tenant has never emitted; `tests/ops/test_blast_radius.py`
    # only ever fed the classifier that synthetic shape, which is why a fully
    # green suite coexisted with a structurally unreachable Tier A. Deriving the
    # answer from the key's own form needs no list to keep in sync.
    # ⛔ `field_path == top_key`, not `top_key in ...`. The structural keys are
    # structural AT THEIR OWN LEVEL only. Matching on the top key demoted every
    # path BENEATH them too, and two of them are threshold maps by definition:
    # `Profiles map[string]map[string]ScheduledValue` (`pkg/config/types.go`)
    # makes `profiles.<name>.<metric>` a threshold, and `tenants.<id>.<metric>`
    # is per-tenant thresholds outright. Measured: six real paths went A → B,
    # i.e. the demotion re-created the #1419 defect inside its own fix.
    if not top_key.startswith("_") and field_path not in PLATFORM_DOCUMENT_KEYS:
        return "A"

    # Fallback is Tier B, not Tier C. Tier C's rendered claim is "no threshold /
    # routing / alerting impact" — a positive assertion of harmlessness, so it
    # has to be the provable category (METADATA_FIELDS above), never the bucket
    # that catches everything nobody classified. An unrecognised `_`-prefixed
    # key (`_silent_mode`, `_state_*`, whatever is added next) gets listed
    # rather than declared safe.
    return "B"


def diff_configs(base: dict, pr: dict) -> dict:
    """Diff two effective config dicts.

    Returns dict with:
      added: {key: new_value}
      removed: {key: old_value}
      changed: {key: {"base": old, "pr": new}}
    """
    flat_base = flatten_dict(base)
    flat_pr = flatten_dict(pr)

    all_keys = set(flat_base.keys()) | set(flat_pr.keys())
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, dict] = {}

    for key in sorted(all_keys):
        in_base = key in flat_base
        in_pr = key in flat_pr

        if in_base and not in_pr:
            removed[key] = flat_base[key]
        elif not in_base and in_pr:
            added[key] = flat_pr[key]
        elif flat_base[key] != flat_pr[key]:
            changed[key] = {"base": flat_base[key], "pr": flat_pr[key]}

    return {"added": added, "removed": removed, "changed": changed}


def classify_diff(diff: dict) -> dict[str, list[dict]]:
    """Classify all diff entries into tiers.

    Returns: {"A": [...], "B": [...], "C": [...]}
    Each entry: {"field": str, "action": "added"|"removed"|"changed", "detail": ...}
    """
    tiers: dict[str, list[dict]] = {"A": [], "B": [], "C": []}

    for key, val in diff["added"].items():
        # Reef 6: a missing key → empty `_custom_alerts: []` is semantically a
        # no-op (no recipes either way). Treat None ≡ [] so it produces no diff
        # noise (would otherwise flag Tier A "added").
        # ⛔ BOTH recipe fields, per `RECIPE_LIST_FIELDS`. The literal here
        # named only one while the `changed` branch below already used the
        # constant, so an empty `_custom_alerts_resolution` appearing or
        # vanishing produced a Tier A entry that the identical shape on the
        # sibling field did not — the "treating a half" the constant's own
        # comment warns about, two branches from it.
        if key in RECIPE_LIST_FIELDS and not val:
            continue
        tier = classify_field(key)
        tiers[tier].append({
            "field": key,
            "action": "added",
            "detail": {"pr": val},
        })

    for key, val in diff["removed"].items():
        if key in RECIPE_LIST_FIELDS and not val:
            continue
        tier = classify_field(key)
        tiers[tier].append({
            "field": key,
            "action": "removed",
            "detail": {"base": val},
        })

    for key, val in diff["changed"].items():
        tier = classify_field(key)
        # N3: a pure reorder of the recipe lists has zero alerting impact (rules
        # are vectorized by shape, order-independent). Downgrade A → C so a
        # cosmetic reorder doesn't flag substantive / flip the report to
        # summary-mode and bury real changes. Only downgrades when the recipe
        # multiset is byte-identical, so a real change is never hidden.
        # ⛔ BOTH recipe fields, not just `_custom_alerts`. `describe_tenant.py`
        # writes `_custom_alerts` and `_custom_alerts_resolution` from the same
        # `resolved` list in the same order, so a reorder always reorders both.
        # Promoting the resolution field into Tier A (same PR) therefore made
        # this downgrade UNREACHABLE on the real path — the tenant stayed Tier A
        # through the sibling field — while a test went on pinning the synthetic
        # shape where only `_custom_alerts` moves. Measured before this fix:
        # realistic reorder → Tier A, synthetic reorder → Tier C.
        if key in RECIPE_LIST_FIELDS and _custom_alerts_reorder_only(
            val.get("base"), val.get("pr")
        ):
            tier = "C"
        tiers[tier].append({
            "field": key,
            "action": "changed",
            "detail": val,
        })

    return tiers


def compute_blast_radius(base_data: dict, pr_data: dict) -> dict:
    """Compute blast radius report comparing base vs PR effective configs.

    Args:
        base_data: dict from describe-tenant --all (base branch)
        pr_data: dict from describe-tenant --all (PR branch)

    Returns: structured report dict
    """
    all_tenants = sorted(set(base_data.keys()) | set(pr_data.keys()))

    tenant_results: list[dict] = []
    summary = {
        "total_tenants_scanned": len(all_tenants),
        "affected_tenants": 0,
        "tier_a_tenants": 0,
        "tier_b_tenants": 0,
        "tier_c_only_tenants": 0,
        "new_tenants": 0,
        "removed_tenants": 0,
    }

    for tid in all_tenants:
        base_info = base_data.get(tid)
        pr_info = pr_data.get(tid)

        # New tenant (added in PR)
        if base_info is None:
            summary["new_tenants"] += 1
            summary["affected_tenants"] += 1
            tenant_results.append({
                "tenant_id": tid,
                "status": "new",
                "highest_tier": "A",
                "tiers": {"A": [], "B": [], "C": []},
            })
            continue

        # Removed tenant (deleted in PR)
        if pr_info is None:
            summary["removed_tenants"] += 1
            summary["affected_tenants"] += 1
            tenant_results.append({
                "tenant_id": tid,
                "status": "removed",
                "highest_tier": "A",
                "tiers": {"A": [], "B": [], "C": []},
            })
            continue

        # Compare merged_hash for quick skip
        base_hash = base_info.get("merged_hash", "")
        pr_hash = pr_info.get("merged_hash", "")

        if base_hash and pr_hash and base_hash == pr_hash:
            continue  # No effective change

        # Deep diff effective configs
        base_eff = base_info.get("effective_config", {})
        pr_eff = pr_info.get("effective_config", {})

        if base_eff == pr_eff:
            continue  # Identical after merge (hash collision edge case)

        diff = diff_configs(base_eff, pr_eff)
        tiers = classify_diff(diff)

        # Determine highest tier
        if tiers["A"]:
            highest = "A"
            summary["tier_a_tenants"] += 1
        elif tiers["B"]:
            highest = "B"
            summary["tier_b_tenants"] += 1
        elif tiers["C"]:
            highest = "C"
            summary["tier_c_only_tenants"] += 1
        else:
            continue  # No meaningful diff

        summary["affected_tenants"] += 1
        tenant_results.append({
            "tenant_id": tid,
            "status": "changed",
            "highest_tier": highest,
            "tiers": tiers,
        })

    return {
        "summary": summary,
        "tenants": tenant_results,
    }


# ---------------------------------------------------------------------------
# Markdown PR comment generation
# ---------------------------------------------------------------------------

# GitHub enforces a 65,536 character ceiling on issue/PR comment bodies.
# Comments exceeding the limit are silently rejected (422 Unprocessable Entity)
# — the workflow would "succeed" but the comment never appears. We leave a
# headroom of ~5KB to absorb the wrapping marker + footer the workflow adds.
GITHUB_COMMENT_HARD_LIMIT = 65_536
COMMENT_SAFETY_LIMIT = 60_000

# When more than this many Tier A+B tenants are affected, switch the comment
# to summary-only mode (list tenant IDs without per-field diffs). The full
# diff is always available as the `blast-radius-report` workflow artifact.
SUMMARY_MODE_TENANT_THRESHOLD = 50
# Cap on individual tenant IDs listed even in summary-only mode. Anything
# beyond this gets collapsed to a "+N more" tail; the artifact remains
# authoritative.
SUMMARY_MODE_LIST_CAP = 200

# Silenced/deleted paging recipes are the loudest thing this report can say,
# so they get their own (larger) cap ahead of the tier lists. Bounded rather
# than unbounded because the whole body is hard-truncated later regardless —
# an unbounded list pushed the body to COMMENT_SAFETY_LIMIT and the generic
# truncator then cut the silenced names AND the entire tier-A tenant list.
# 500 is not arbitrary: measured, 500 silenced + SUMMARY_MODE_LIST_CAP tier
# ids renders ~15.5 KB, about a quarter of COMMENT_SAFETY_LIMIT, and stays
# flat at 1000 / 1500 / 3000 affected tenants.
# `test_the_silenced_count_survives_hard_truncation` pins the guarantee that
# actually matters — the COUNT and the artifact pointer, not the names.
SILENCED_LIST_CAP = 500


def _recipe_names(lst: Any) -> list[str]:
    """Pull the `name` of each recipe dict in a (possibly None) list."""
    out: list[str] = []
    for r in (lst or []):
        if isinstance(r, dict):
            out.append(str(r.get("name", "?")))
    return out


# Mirror _lib_validation._DISABLED_VALUES / Go IsDisabledValue WITHOUT importing
# (blast_radius is the intentionally zero-dep "ultralight scanner", P2 — keep in
# sync). The exporter's custom-alert opt-out (app/pkg/config/custom_alert.go)
# parses value:severity then checks isDisabled(value-part) against this set, so a
# bare `== "disable"` would miss `off` / `disabled` / `false` and any
# `:severity`-suffixed form — under-flagging a real silencing (R2 self-review).
_DISABLED_VALUES = frozenset({"disable", "disabled", "off", "false"})


def _threshold_disabled(recipe: dict) -> bool:
    """A custom-alert recipe whose threshold is a three-state opt-out.

    Mirrors custom_alert.go: split the optional `:severity` first, then test the
    value part against the full disabled set (NOT just the literal "disable").
    """
    raw = str(recipe.get("threshold", "")).strip()
    value = raw.rsplit(":", 1)[0] if ":" in raw else raw
    return value.strip().lower() in _DISABLED_VALUES


def _pages(recipe: dict) -> bool:
    """Would this recipe actually reach a pager?

    ⛔ The SAME two conditions `_is_silencing` transitions between, expressed as
    a state instead of a transition, so a group of same-named recipes can be
    compared without pairing them up. Pairing is what went wrong: the
    cross-product `any(_is_silencing(b, p) …)` fires whenever ANY base recipe
    can be read against ANY pr recipe as a silencing, and under ADR-024 UNION
    inheritance a tenant REACHES THIS TOOL carrying an inherited recipe and its
    own recipe under one name.

    ⚠️ "Reaches this tool", not "legitimately", which is what this said before.
    The duplicate is real on the input side — `describe_tenant` merges by union
    and emits both — but the CI compiler does not accept it: `loader.py`'s
    `build_shapes` quarantines one of the two into `skipped` with the reason
    `duplicate custom-alert name … names must be unique per tenant` (pinned by
    `tests/dx/test_compile_custom_alerts.py`). So this is a config error the
    customer will be told about, and handling it here is still required —
    blast_radius runs on the PR, BEFORE the compiler has said anything — but
    calling it legitimate told the next reader the platform is fine with it.

    Measured end-to-end through describe_tenant: an
    inherited `disable` beside an own `9:warning → 8:warning` produced
    "SILENCED ['shared']" while neither recipe is silencing on its own — an
    invented alarm in the loudest line this report can print.
    """
    return not _threshold_disabled(recipe) and str(recipe.get("mode")) != "silent"


def _group_became_silent(base_group: list[dict], pr_group: list[dict]) -> bool:
    """This name used to reach a pager and no longer does.

    Order-independent and duplicate-safe.

    ⚠️ History, because the alternative is gone rather than merely unused: an
    earlier `_is_silencing(base_recipe, pr_recipe)` asked the same question
    pairwise, and a docstring here claimed the two agreed "in every direction",
    listing four transitions that happen to agree and presenting them as the
    whole space. Measured over the full single-recipe space (9 states × 9
    states = 81 pairs) they disagreed on **12**, all of one shape: the base
    recipe already does not page and the PR recipe acquires the OTHER silencing
    property — the pairwise form called that a silencing, though no pager was
    lost. That function had no caller left once the report switched to
    comparing groups, and a dead predicate beside a live one reads like a
    second opinion, so it was removed rather than kept as documentation.

    ⛔ The property this one owes is two-sided and is pinned that way by
    `test_a_group_is_silenced_only_when_its_last_pager_stops`, with groups of
    more than one member: at size one `any` and `all` are the same function,
    so a single-recipe table cannot see the group semantics at all.
    """
    return any(_pages(r) for r in base_group) and not any(_pages(r) for r in pr_group)


def _custom_alerts_reorder_only(base: Any, pr: Any) -> bool:
    """True if base/pr hold the SAME recipes ignoring order (a pure reorder).

    Recipe rules are vectorized by shape signature (order-independent), so a
    reorder has zero alerting impact — but flatten_dict sees the reordered list
    as 'changed'. Without this, a YAML-sorter reorder flags Tier A and (at
    scale) flips the report to summary-mode, burying real changes (N3).

    ⛔ Both sides must be lists. Without the check the canonicaliser iterates a
    string character by character, so `"abc"` vs `"cba"` came back "reorder
    only" ⇒ Tier C ⇒ "no threshold/routing/alerting impact" on a value that
    changed. Not producer-reachable today (both recipe fields are lists), but
    the predicate is now reached for `_custom_alerts_resolution` as well, and
    "the producer happens not to do that" is not a property of this function.
    """
    if not isinstance(base, list) or not isinstance(pr, list):
        return False

    def _canon(lst: Any) -> list[str]:
        return sorted(
            json.dumps(r, sort_keys=True, default=str) for r in (lst or [])
        )
    return _canon(base) == _canon(pr)


def silenced_recipe_names(entry: dict) -> list[str]:
    """Recipes this `_custom_alerts` entry silences or DELETES.

    ⛔ One implementation, called by both renderers. `_silenced_tenants` used to
    carry its own copy that counted deletions while `_summarize_custom_alerts`
    counted only in-place silencing — so a deleted recipe was a :rotating_light:
    in summary mode and a bare `-['name']` in full detail. The docstring of the
    copy asserted the two "cannot disagree", which is the shape where a second
    implementation is worse than none: it came with a promise.
    """
    detail = entry.get("detail") or {}
    # ⛔ Group by name instead of last-wins `{name: recipe}`. Duplicate names
    # collapse under a dict, and the two sides can then keep DIFFERENT
    # survivors — so a byte-identical multiset (a pure reorder, which the
    # tiering calls "provably harmless") could raise the SILENCE/DELETE alarm.
    # ⚠️ Duplicate names are not hypothetical: ADR-024 inheritance is a UNION,
    # so a tenant carries an inherited recipe and its own recipe under one name
    # (that is what `_custom_alerts_resolution` exists to show). ⛔ Not the same
    # as "the platform accepts them" — see `_pages` for the measurement: the CI
    # compiler quarantines one of the two. What matters for THIS tool is only
    # that the shape arrives here, on a PR, before any of that has run.
    base = _by_name(detail.get("base"))
    pr = _by_name(detail.get("pr"))
    # ⛔ A RENAME is not a silencing: the pager moved, it did not stop. Treating
    # every vanished name as silenced made `old-name-page → new-name-page` fire
    # the same :rotating_light: as a real delete, and in summary mode the two
    # are visually identical — so an ordinary refactor cries wolf on the
    # loudest line this report prints. Before the delete-counts-as-silencing
    # fix a rename was merely invisible here; that fix traded a false negative
    # for a false positive, which on this particular banner is the worse trade.
    # ⚠️ Deliberately conservative: the body is compared verbatim minus `name`,
    # so a rename that ALSO retunes or disables the recipe still fires.
    # ⛔ Every body still present on the PR side, NOT only those under a name
    # that is new. The `if n not in base` filter made the oracle ask "did this
    # body land under a name I have never seen", and a rename ONTO an existing
    # name answers no: base `{alpha: page, beta: other}` renamed to
    # `{beta: [other, page]}` left `moved` empty, so `alpha` was announced as
    # SILENCED — the same false :rotating_light: on the loudest line in this
    # report that the paragraph above exists to remove, reintroduced by the
    # fix for it. The question this set answers is "does this exact body still
    # exist anywhere in the PR", which is what a rename means; the name it
    # arrived under is not part of that question.
    moved = {_recipe_body(r) for n in pr for r in pr[n]}

    def _lost_a_pager(name: str) -> bool:
        """Did a paging recipe under this name actually STOP, not just move?

        ⛔ ONE predicate for both branches below. It used to live inline in
        `removed` only, and `disabled` never consulted `moved` at all — so the
        rename false-positive this whole paragraph exists to remove came back
        through the other branch: base `alpha` pages, the PR renames that body
        to `beta` AND leaves a silent recipe under `alpha`, so `alpha` is in
        BOTH sides, misses `removed` entirely, and `_group_became_silent` says
        page → not-page. Reproduced before the fix: `['alpha']`, with the pager
        still live under `beta`. Naming the predicate is the point — two copies
        of this question is how one of them got fixed and the other did not.
        """
        return any(_pages(r) and _recipe_body(r) not in moved for r in base[name])

    removed = {n for n in base if n not in pr and _lost_a_pager(n)}
    # ⛔ Compare the GROUPS as states, never the cross-product of their members.
    # `any(_is_silencing(b, p) for b in base[n] for p in pr[n])` pairs every
    # base recipe with every pr recipe, so the inherited `disable` beside an own
    # `9:warning → 8:warning` was read as a silencing and printed one —
    # reproduced end to end through describe_tenant, with both recipes
    # individually not silencing. `_same_multiset` was in front of it as a
    # guard, which made a pure reorder inert but did nothing for this shape.
    disabled = {n for n in (set(base) & set(pr))
                if _group_became_silent(base[n], pr[n]) and _lost_a_pager(n)}
    return sorted(removed | disabled)


def _by_name(recipes: Any) -> dict[str, list[dict]]:
    """name -> every recipe carrying it (never last-wins)."""
    out: dict[str, list[dict]] = {}
    for r in (recipes or []):
        if isinstance(r, dict):
            out.setdefault(str(r.get("name")), []).append(r)
    return out


def _printable(text: str | None) -> str | None:
    """Free-form display text that is guaranteed to survive a strict encode.

    Undecodable bytes arrive as lone surrogates (`surrogateescape` on argv);
    `backslashreplace` turns each one into a `\\udcXX` escape, which is ugly,
    lossless enough for a header, and — unlike the surrogate — encodable.

    ⚠️ NOT "the backslash form git would have produced", which is what this
    docstring used to claim. The two forms are different and the difference is
    checkable: git's `core.quotePath` writes OCTAL BYTES (`\\264\\372`), Python
    writes the UNICODE ESCAPE for the surrogate (`\\udcb4\\udcfa`). The
    workflow comment on the header line describes git's form, so the two sides
    of this one PR contradicted each other. Nothing downstream parses either
    form — the value is display-only — so the fix is the sentence, not the
    handler. The guarantee this function actually makes is the first line.
    """
    if text is None:
        return None
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _recipe_body(recipe: dict) -> str:
    """Canonical recipe content with the name removed — the rename oracle."""
    return json.dumps({k: v for k, v in recipe.items() if k != "name"},
                      sort_keys=True, default=str)


def _same_multiset(a: list[dict], b: list[dict]) -> bool:
    def _canon(lst: list[dict]) -> list[str]:
        return sorted(json.dumps(r, sort_keys=True, default=str) for r in lst)
    return _canon(a) == _canon(b)


def _summarize_custom_alerts(entry: dict) -> str:
    """Recipe-level summary for a recipe-list diff entry (F2).

    flatten_dict keeps the whole list as one opaque value, so the raw diff would
    dump both full lists (a ~1.7KB blob for one changed recipe → review fatigue,
    reviewers skip it). Instead surface WHICH recipes changed, by name.

    ⚠️ Reached for BOTH `RECIPE_LIST_FIELDS`. Routing only `_custom_alerts` here
    left `_custom_alerts_resolution` — promoted into Tier A by this same PR — on
    the generic renderer, i.e. dumping exactly the blob this function exists to
    avoid, and burning one of the per-tenant line slots to do it.
    """
    field = entry.get("field", "_custom_alerts")
    action = entry["action"]
    detail = entry.get("detail") or {}
    # ⛔ Computed BEFORE the early returns, for every action. The previous
    # version called the shared predicate only on the `changed` branch, so the
    # plainest silencing there is — the tenant deleting its whole
    # `_custom_alerts:` block, which arrives as action="removed" — produced a
    # :rotating_light: in summary mode and a bare "_(removed)_ N recipe(s)" in
    # full detail. The docstring of `_silenced_tenants` claimed the two modes
    # could not disagree BECAUSE of this shared call, while the call was not on
    # the path that disagreed. ⚠️ `added` is included deliberately and is inert
    # by construction (no base side ⇒ nothing removed, nothing disabled); it is
    # not special-cased, so there is no branch to get wrong later.
    silenced = silenced_recipe_names(entry) if field == "_custom_alerts" else []
    mark = (f"  :warning: **SILENCED (disabled / deleted / mode→silent): "
            f"{silenced}**") if silenced else ""
    if action == "added":
        names = _recipe_names(detail.get("pr"))
        return f"`{field}`: _(new)_ {len(names)} recipe(s): {names}{mark}"
    if action == "removed":
        names = _recipe_names(detail.get("base"))
        return f"`{field}`: _(removed)_ {len(names)} recipe(s): {names}{mark}"
    # changed: name-level delta between base and pr lists
    base, pr = detail.get("base") or [], detail.get("pr") or []
    # ⛔ `_by_name`, not a last-wins `{name: recipe}`. The same last-wins
    # construct sat in `silenced_recipe_names` and was fixed there for exactly
    # this reason — "fixed the cited instance, not the class" inside one
    # module. (No line distance: the referent is named, because a counted
    # one goes stale on the next edit and nothing recounts it.)
    # It made a byte-identical duplicate-name reorder render `~['a']`, i.e.
    # "this recipe was modified", on the case the tiering calls harmless.
    bmap = _by_name(base)
    pmap = _by_name(pr)
    added = sorted(set(pmap) - set(bmap))
    removed = sorted(set(bmap) - set(pmap))
    modified = sorted(n for n in (set(bmap) & set(pmap))
                      if not _same_multiset(bmap[n], pmap[n]))
    parts = []
    if added:
        parts.append(f"+{added}")
    if removed:
        parts.append(f"-{removed}")
    if modified:
        parts.append(f"~{modified}")
    delta = "; ".join(parts) if parts else "reordered/other (no name-level change)"
    # ⛔ Reuse `mark`; do not respell it. The two spellings were identical
    # apart from leading space, which is exactly how the wording of the
    # loudest line in this report drifts between two renderers.
    return f"`{field}` changed (count {len(base)}→{len(pr)}): {delta}{mark}"


PER_TENANT_LINE_CAP = 10


def _entry_is_silencing(entry: dict) -> bool:
    return (entry.get("field") == "_custom_alerts"
            and bool(silenced_recipe_names(entry)))


def _tenant_change_summary(tenant: dict,
                           cap: int = PER_TENANT_LINE_CAP) -> str:
    """One-line summary for a tenant's changes (used in full-detail mode).

    ⛔ Two properties the plain `lines[:cap]` did not have, both measured on a
    real `conf.d` edit:

    1. **The silencing highlight is never the line that gets dropped.** Entry
       order is added → removed → changed, and a silencing is always a
       `changed` `_custom_alerts` entry, i.e. LAST. Any tenant gaining ≥cap
       Tier-A keys in the same PR buried it — while the sibling renderer's
       comment (`_render_summary_only`) says in as many words that this
       highlight "must not be the thing that truncation drops first". That
       argument was applied to summary mode only; full detail is the mode the
       common case (≤ the tenant threshold) actually renders.
    2. **Truncation is disclosed.** Halving eleven real thresholds rendered ten
       bullets and dropped the eleventh with no marker, so the reader could not
       tell a complete list from a partial one.
    """
    priority: list[str] = []
    rest: list[str] = []
    for entry in tenant["tiers"]["A"]:
        if entry.get("field") in RECIPE_LIST_FIELDS:
            line = _summarize_custom_alerts(entry)
            (priority if _entry_is_silencing(entry) else rest).append(line)
            continue
        detail = entry.get("detail", {})
        if entry["action"] == "changed":
            rest.append(f"`{entry['field']}`: {detail.get('base')} → {detail.get('pr')}")
        elif entry["action"] == "added":
            rest.append(f"`{entry['field']}`: _(new)_ {detail.get('pr')}")
        else:
            rest.append(f"`{entry['field']}`: _(removed)_ {detail.get('base')}")
    for entry in tenant["tiers"]["B"]:
        if entry["action"] == "changed":
            detail = entry.get("detail", {})
            rest.append(f"`{entry['field']}`: {detail.get('base')} → {detail.get('pr')}")
        else:
            rest.append(f"`{entry['field']}`: _{entry['action']}_")

    # ⛔ Order is the mechanism, not a separate budget. An earlier version
    # reserved room (`cap - len(priority)`) for the priority lines, which reads
    # like a second safeguard and is not one: `priority` holds at most ONE entry
    # (a tenant has a single `_custom_alerts` field, and a key lands in exactly
    # one of added/removed/changed), so the reserved-room form and this one are
    # behaviourally identical for every input the producer can build. Mutating
    # it away left the suite green — correctly — and a branch nothing can
    # distinguish is decoration, so it is gone rather than pinned by a synthetic
    # test.
    shown = (priority + rest)[:cap]
    dropped = (len(priority) + len(rest)) - len(shown)
    out = [f"  - {line}" for line in shown]
    if dropped:
        out.append(
            f"  - _…and {dropped} further change(s) not shown — the "
            f"`blast-radius-report` artifact has the full per-field diff_")
    return "\n".join(out)


def _render_header(report: dict, changed_files: str | None) -> list[str]:
    """Shared header + summary table used by both full and summary rendering."""
    s = report["summary"]
    lines: list[str] = []
    # ⛔ Unconditional. `_comment_header` already answers the falsy case, and
    # its own docstring claims "ONE implementation, two call sites" — while
    # this else-branch held a THIRD copy of the same string, so rewording the
    # header in one place would have left the two branches disagreeing with
    # nothing to notice. The duplicate the docstring warns about was in its
    # caller.
    lines.append(_comment_header(changed_files))

    lines.append("| Metric | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Total tenants scanned | {s['total_tenants_scanned']} |")
    lines.append(f"| Affected tenants | {s['affected_tenants']} |")
    if s["tier_a_tenants"]:
        lines.append(f"| Tier A (threshold/routing) | {s['tier_a_tenants']} |")
    if s["tier_b_tenants"]:
        lines.append(f"| Tier B (other alerting) | {s['tier_b_tenants']} |")
    if s["tier_c_only_tenants"]:
        lines.append(f"| Tier C (format-only) | {s['tier_c_only_tenants']} |")
    if s["new_tenants"]:
        lines.append(f"| New tenants | {s['new_tenants']} |")
    if s["removed_tenants"]:
        lines.append(f"| Removed tenants | {s['removed_tenants']} |")
    lines.append("")
    return lines


def _render_full_detail(
    report: dict,
    changed_files: str | None,
    artifact_hint: str | None,
) -> str:
    """Full-detail rendering: summary table + Tier A+B per-field diffs."""
    lines = _render_header(report, changed_files)

    tier_ab_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] in ("A", "B")
    ]
    if tier_ab_tenants:
        lines.append("<details>")
        lines.append(f"<summary>Substantive changes: {len(tier_ab_tenants)} tenants</summary>\n")
        status_badge = {"changed": "", "new": " (NEW)", "removed": " (REMOVED)"}
        for t in tier_ab_tenants:
            badge = status_badge.get(t["status"], "")
            lines.append(f"- **{t['tenant_id']}**{badge}")
            detail = _tenant_change_summary(t)
            if detail:
                lines.append(detail)
        lines.append("\n</details>\n")

    tier_c_tenants = [
        t for t in report["tenants"]
        if t["highest_tier"] == "C"
    ]
    if tier_c_tenants:
        lines.append(
            f"Format-only changes: {len(tier_c_tenants)} tenants "
            f"(no threshold/routing/alerting impact)"
        )

    if artifact_hint:
        lines.append("")
        lines.append(f"_{artifact_hint}_")

    return "\n".join(lines)


def _silenced_tenants(report: dict) -> list[tuple[str, list[str]]]:
    """(tenant_id, recipe names) for tenants that silenced or DELETED a recipe.

    ⚠️ Scope is `_custom_alerts` entries only. A tenant removed outright is the
    most complete way to stop its pagers, and it does NOT appear here — those
    carry empty `tiers`, so there is nothing for this to read. The summary
    header's `Removed tenants` count is what covers them today, which is why
    this is a narrower promise than "every tenant that silenced a pager".

    ⛔ Deletion counts, and it counts the same way in both modes. `_is_silencing`
    alone only compares recipes present by name on BOTH sides, because it was
    written to unmask "a delete wearing a modified disguise" — so an actual
    delete, the plainest way to stop a pager, was the one shape it never saw.
    ⚠️ This docstring used to claim the two modes "cannot disagree about what
    counts as silenced" while THIS function carried a second, wider copy of the
    rule: a deleted recipe was a :rotating_light: here and a bare `-['name']`
    in full detail. Both now call `silenced_recipe_names`, so the claim is
    carried by a shared call rather than by a sentence.
    """
    out: list[tuple[str, list[str]]] = []
    for tenant in report.get("tenants", []):
        # ⛔ Tier A only, and that is a DERIVED bound rather than a shortcut.
        # `classify_field("_custom_alerts")` is "A" unconditionally, and the
        # reorder downgrade is the only thing that moves it — but a reorder-only
        # entry has an identical recipe multiset, hence identical per-name
        # groups, hence no silencing, by construction (also brute-forced: 0 of
        # 20000 random reorder pairs). So no report this producer can build
        # carries a silencing `_custom_alerts` entry outside Tier A, and
        # `test_a_silencing_entry_never_lands_outside_tier_a` fails if that
        # stops being true.
        # ⚠️ An earlier version iterated every tier the report happened to
        # carry and claimed that "removes the enumeration instead of pinning
        # it". It did neither: putting the literal tuple back was green,
        # because the test that was supposed to hold the wider contract looped
        # over the same three keys. The wider scan also had a cost — Tier B and
        # C entries are rendered by paths that never print the highlight, so it
        # made the two renderers disagree on exactly the shapes it enabled.
        for entry in (tenant.get("tiers") or {}).get("A") or []:
            if entry.get("field") != "_custom_alerts":
                continue
            names = silenced_recipe_names(entry)
            if names:
                out.append((tenant["tenant_id"], names))
    return out


def _comment_header(changed_files: str | None) -> str:
    """The comment's first line. ⛔ ONE implementation, two call sites.

    The zero-change branch grew its own copy of this f-string, so the two could
    drift with nothing to notice: renaming the wording in one of them left the
    whole suite green (measured). Same judgement, one implementation — the rule
    this module already applied to `silenced_recipe_names`, applied late here.
    """
    if changed_files:
        return f"### Blast Radius: this PR modifies `{changed_files}`\n"
    return "### Blast Radius Report\n"


def _render_summary_only(
    report: dict,
    changed_files: str | None,
    artifact_hint: str | None,
    list_cap: int = SUMMARY_MODE_LIST_CAP,
) -> str:
    """Summary-only rendering: tenant IDs grouped by tier, no per-field diffs.

    Designed to stay well under GitHub's 65,536-char limit even with 1000+
    affected tenants. The full per-field diff is always available as the
    `blast-radius-report` workflow artifact (authoritative source).
    """
    lines = _render_header(report, changed_files)

    tier_a = [t for t in report["tenants"] if t["highest_tier"] == "A"]
    tier_b = [t for t in report["tenants"] if t["highest_tier"] == "B"]
    tier_c = [t for t in report["tenants"] if t["highest_tier"] == "C"]

    lines.append(
        "> :warning: Affected tenant count exceeds the inline-detail threshold "
        f"({SUMMARY_MODE_TENANT_THRESHOLD}). Showing tenant list only — "
        "per-field diffs are available in the workflow artifact."
    )
    lines.append("")

    def _list_block(header: str, tenants: list[dict]) -> list[str]:
        if not tenants:
            return []
        block = [f"<details>", f"<summary>{header}: {len(tenants)} tenants</summary>\n"]
        status_badge = {"changed": "", "new": " (NEW)", "removed": " (REMOVED)"}
        shown = tenants[:list_cap]
        for t in shown:
            badge = status_badge.get(t["status"], "")
            block.append(f"- `{t['tenant_id']}`{badge}")
        if len(tenants) > list_cap:
            block.append(f"- _…and {len(tenants) - list_cap} more (see artifact)_")
        block.append("\n</details>\n")
        return block

    # ⛔ Silenced recipes survive summary mode, above the tier lists and
    # under their OWN cap (`SILENCED_LIST_CAP`), which is larger than the
    # tier caps rather than absent — the truncation below says so when it
    # bites. This line read `uncapped` while the code three statements down
    # sliced the list.
    # `_summarize_custom_alerts` builds the ":warning: SILENCED" highlight, and
    # its only caller is `_tenant_change_summary` — which summary mode never
    # reaches. Before #1419 that was harmless: a `_defaults.yaml` bump left
    # every inheriting tenant in Tier C, so `substantive_count` stayed near
    # zero and this mode was rare. Classifying thresholds as Tier A (correctly)
    # pushed routine defaults bumps over the threshold, and a PR that bumps a
    # default AND silences one tenant's pager then rendered the silencing
    # NOWHERE — no warning, no recipe name, and past `list_cap` not even the
    # tenant id. The highlight this file calls its "semantic camouflage"
    # defense must not be the thing that truncation drops first.
    silenced = _silenced_tenants(report)
    if silenced:
        # ⚠️ The COUNT is the guarantee, not the list. An earlier version of
        # this block said "listed in full because truncation must never hide
        # this" — measured false: `generate_pr_comment` hard-truncates the
        # finished body at COMMENT_SAFETY_LIMIT with no idea what it is cutting,
        # so at 5000 silenced tenants 3395 of them were dropped mid-list under a
        # generic "output truncated" notice. A bounded list keeps the headline
        # number and the artifact pointer intact, which is what a reader needs
        # to know they must go look.
        lines.append(
            f"> :rotating_light: **{len(silenced)} tenant(s) SILENCE or DELETE "
            f"a paging recipe in this PR.** Full list in the workflow artifact."
        )
        for tenant_id, names in silenced[:SILENCED_LIST_CAP]:
            lines.append(f"> - `{tenant_id}`: {names}")
        if len(silenced) > SILENCED_LIST_CAP:
            lines.append(
                f"> - _…and {len(silenced) - SILENCED_LIST_CAP} more silenced "
                f"tenant(s) — see artifact_"
            )
        lines.append("")

    # ⛔ The heading names what the GROUP contains, which is not what the
    # summary table's `Tier A` row counts. Grouping is by `highest_tier`, and
    # new/removed tenants carry a hardcoded `"A"`, while `tier_a_tenants`
    # excludes them — measured, one comment showed `| Tier A | 60 |` in the
    # table and `Tier A: 64 tenants` in the fold immediately below it, with no
    # way for a reviewer to tell which number was the real one. Renaming the
    # heading removes the contradiction without touching the counts, which
    # feed the summary-mode threshold and are a separate decision (see the
    # follow-up ticket on `substantive_count`).
    lines.extend(_list_block(
        "Tier A (threshold/routing) + new/removed tenants", tier_a))
    lines.extend(_list_block("Tier B (other alerting)", tier_b))

    if tier_c:
        lines.append(
            f"Format-only changes: {len(tier_c)} tenants "
            f"(no threshold/routing/alerting impact)"
        )

    if artifact_hint:
        lines.append("")
        lines.append(f"_{artifact_hint}_")

    return "\n".join(lines)


def generate_pr_comment(
    report: dict,
    changed_files: str | None = None,
    *,
    artifact_hint: str | None = None,
) -> str:
    """Generate GitHub PR comment markdown from blast radius report.

    Output is guaranteed to stay under GitHub's 65,536-char comment ceiling:

    1. If Tier A+B affected count > SUMMARY_MODE_TENANT_THRESHOLD (50), flip
       to summary-only rendering up front.
    2. If the rendered body still exceeds COMMENT_SAFETY_LIMIT (60,000),
       re-render in summary-only mode.
    3. If even summary-only exceeds the limit (edge case with thousands of
       very long tenant IDs), hard-truncate and tail with a visible marker.

    Args:
        report: Structured report from :func:`compute_blast_radius`.
        changed_files: Optional comma-separated list of changed conf.d/ files
            to include in the header.
        artifact_hint: Optional human-readable hint appended at the end
            pointing reviewers at the full JSON artifact. Example:
            ``"Full per-tenant diff available in the `blast-radius-report`
            artifact on this workflow run."``
    """
    s = report["summary"]

    if s["affected_tenants"] == 0:
        # ⛔ Keep the changed-files header. Dropping it here meant the single
        # comment that makes the strongest positive claim — "nothing changed"
        # — was also the one that told the reviewer least: they could not even
        # see which file the PR touched, so there was nothing to notice the
        # claim against. ⚠️ This does not make the claim true; a platform-level
        # `_routing_defaults` / `state_filters` edit reaches no tenant's
        # effective config at all and still lands here (measured over four
        # shapes). That root cause is upstream in `describe_tenant`'s merge and
        # is tracked separately; showing the file is the part this tool can do.
        head = _comment_header(changed_files) + "\n"
        return (
            head
            + "No effective tenant config changes detected in this PR.\n"
        )

    substantive_count = s["tier_a_tenants"] + s["tier_b_tenants"]
    force_summary = substantive_count > SUMMARY_MODE_TENANT_THRESHOLD

    if force_summary:
        body = _render_summary_only(report, changed_files, artifact_hint)
    else:
        body = _render_full_detail(report, changed_files, artifact_hint)
        # Even below the tenant threshold, per-field detail can bloat (e.g. a
        # single tenant with 1000 field changes). Fall back to summary-only
        # as a safety net.
        if len(body) > COMMENT_SAFETY_LIMIT:
            body = _render_summary_only(report, changed_files, artifact_hint)

    # Last-resort hard truncation. Should be unreachable under realistic
    # inputs (SUMMARY_MODE_LIST_CAP keeps bodies comfortably below 60KB for
    # any plausible tenant-id length), but we still guard to avoid the GitHub
    # API silently rejecting the comment.
    if len(body) > COMMENT_SAFETY_LIMIT:
        truncation_notice = (
            "\n\n---\n"
            "_Output truncated to fit GitHub's comment length limit. "
            "See the `blast-radius-report` workflow artifact for the complete diff._"
        )
        body = body[: COMMENT_SAFETY_LIMIT - len(truncation_notice)] + truncation_notice

    return body


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def load_effective_json(path: str) -> dict:
    """Load describe-tenant --all JSON output."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Error reading {path}: {exc}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    if not isinstance(data, dict):
        print(f"Error: {path} must contain a JSON object, got {type(data).__name__}", file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Blast Radius diff engine: compare base vs PR effective tenant configs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--base", "-b", required=True,
        help="Path to base branch effective config JSON (from describe-tenant --all --output)",
    )
    parser.add_argument(
        "--pr", "-p", required=True,
        help="Path to PR branch effective config JSON",
    )
    parser.add_argument(
        "--output", "-o", type=str, default=None,
        help="Output file (default: stdout)",
    )
    parser.add_argument(
        "--format", "-f", choices=["json", "markdown"], default="json",
        help="Output format (default: json)",
    )
    parser.add_argument(
        "--changed-files", type=str, default=None,
        help="Comma-separated list of changed conf.d/ files (for PR comment header)",
    )
    parser.add_argument(
        "--artifact-hint", type=str, default=None,
        help=(
            "Optional footer line pointing reviewers at the full JSON artifact "
            "(used when summary-only mode is triggered)."
        ),
    )
    args = parser.parse_args()
    # ⛔ Free-form text from argv may not be valid UTF-8. `git diff -z` hands
    # over raw path bytes, argv decoding turns them into lone surrogates, and
    # the report is written with `write_text(encoding="utf-8")` — strict — so
    # the whole run died with `UnicodeEncodeError: surrogates not allowed` and
    # a traceback pointing here, on exactly the paths the caller passed `-z`
    # for. Measured; the C-quoted ASCII form returns 0 on the same input.
    # ⚠️ Sanitise at INGESTION, not at write time: these two values are
    # display-only, while relaxing the writer would silently corrupt tenant
    # names and thresholds elsewhere in the same document.
    args.changed_files = _printable(args.changed_files)
    args.artifact_hint = _printable(args.artifact_hint)

    # Load inputs
    base_data = load_effective_json(args.base)
    pr_data = load_effective_json(args.pr)

    # Compute blast radius
    report = compute_blast_radius(base_data, pr_data)

    # Format output
    if args.format == "markdown":
        output = generate_pr_comment(
            report,
            changed_files=args.changed_files,
            artifact_hint=args.artifact_hint,
        )
    else:
        output = format_json_report(report)

    # Write output
    if args.output:
        out_path = Path(args.output)
        out_path.write_text(output, encoding="utf-8", newline="\n")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
