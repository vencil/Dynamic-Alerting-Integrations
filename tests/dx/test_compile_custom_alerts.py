"""Tests for the Custom Alerts vectorized compiler (ADR-024 Capability B, #741 S2).

Pinned contracts
----------------
1. **recipe_id is a cross-language slug contract** — matches the shared golden
   vectors (tests/dx/fixtures/recipe_id_vectors.json) that the Go exporter (S3)
   will also assert against. A drift silently breaks every join.
2. **Shape dedup = O(M)** — N tenants on the SAME shape compile to ONE rule;
   same `name` on DIFFERENT shapes compile to TWO rules (no false merge).
3. **Severity union** — a shape emits per-severity branches for exactly the
   severities its covered tenants declared (no forced critical mirror).
4. **Injection defence** — a non-bare metric name / reserved selector label is
   rejected at compile time (HTTP-400-able later via the shared module).
5. **Uniqueness** — duplicate `name` per tenant, and two same-severity alerts on
   one shape per tenant, are rejected (keeps group_left(name) one-to-one).
6. **Scope inheritance** — a domain/platform `_defaults.yaml` recipe lands on
   every subtree tenant (cap count), as ONE shared rule.
7. **--check** — a stale committed pack is flagged; a fresh one passes.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
_LINT = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools")
sys.path.insert(0, _DX)
sys.path.insert(0, _LINT)
sys.path.insert(0, _TOOLS)

import compile_custom_alerts as cc  # noqa: E402
from custom_alerts import shape as shp  # noqa: E402
from custom_alerts import loader as ld  # noqa: E402
from custom_alerts.loader import CustomAlertConfigError  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_VECTORS = _REPO / "tests" / "dx" / "fixtures" / "recipe_id_vectors.json"
_EXAMPLES = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"


# --- helpers ---------------------------------------------------------------
def _write_tree(root: Path, files: dict) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def _alert_names(pack: dict) -> list:
    out = []
    for g in pack["groups"]:
        for r in g.get("rules", []):
            if "alert" in r:
                out.append(r["alert"])
    return out


# --- 1. recipe_id cross-language vector contract ---------------------------
def test_recipe_id_matches_golden_vectors():
    data = json.loads(_VECTORS.read_text(encoding="utf-8"))
    for case in data["vectors"]:
        assert shp.recipe_id(case["input"]) == case["recipe_id"], case["input"]


def test_recipe_id_selector_order_independent():
    a = {"recipe": "threshold", "metric": "m", "op": ">", "window": "1m",
         "selectors": {"alpha": "2", "zeta": "1"}}
    b = {"recipe": "threshold", "metric": "m", "op": ">", "window": "1m",
         "selectors": {"zeta": "1", "alpha": "2"}}
    assert shp.recipe_id(a) == shp.recipe_id(b)


# --- 2. shape dedup (O(M)) -------------------------------------------------
def test_same_shape_multi_tenant_one_rule(tmp_path):
    inst = "{recipe: threshold, name: cpu_hot, metric: node_cpu, op: \">\", window: 5m, threshold: \"80:warning\"}"
    _write_tree(tmp_path, {
        "a.yaml": f"tenants:\n  ta:\n    _custom_alerts:\n      - {inst}\n",
        "b.yaml": f"tenants:\n  tb:\n    _custom_alerts:\n      - {inst}\n",
    })
    pack = cc.build_pack(tmp_path)
    assert pack["_meta"]["shapes"] == 1                 # ONE rule covers both tenants
    names = _alert_names(pack)
    shape_alerts = [n for n in names if n.startswith("Custom_")]
    assert len(shape_alerts) == 1                       # ONE shape rule covers both tenants (O(M))
    assert names.count("CustomRecipeSilent") == 1       # global silent sentinel injected exactly ONCE (S7/S8)


# --- 2b. S7/S8 routing: component label + silent sentinel ------------------
def _alert_rules(pack: dict) -> list:
    return [r for g in pack["groups"] for r in g.get("rules", []) if "alert" in r]


def test_s7s8_component_label_and_silent_sentinel(tmp_path):
    # one page recipe + one silent recipe (different metrics → 2 shapes)
    _write_tree(tmp_path, {
        "a.yaml": (
            "tenants:\n  ta:\n    _custom_alerts:\n"
            '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">", window: 5m, threshold: "80:warning", mode: page}\n'
            '      - {recipe: threshold, name: q_deep, metric: queue_depth, op: ">", window: 5m, threshold: "100:warning", mode: silent}\n'
        ),
    })
    pack = cc.build_pack(tmp_path)
    rules = _alert_rules(pack)

    # A1: every SHAPE alert carries the static component="custom" routing discriminator.
    shape_rules = [r for r in rules if r["alert"].startswith("Custom_")]
    assert shape_rules, "expected shape alerts"
    for r in shape_rules:
        assert r["labels"].get("component") == "custom", r["alert"]

    # silent sentinel: present exactly once, severity=none, scoped to mode="silent",
    # and aggregated by(tenant, name) so the inhibit can match equal:[tenant, name].
    sentinels = [r for r in rules if r["alert"] == "CustomRecipeSilent"]
    assert len(sentinels) == 1
    s = sentinels[0]
    assert s["labels"]["severity"] == "none"
    assert '{component="custom", mode="silent"}' in s["expr"]
    assert "by(tenant, name)" in s["expr"]
    # the sentinel does NOT carry component="custom" → it routes like other platform
    # sentinels (to the default/log receiver), not into the custom firehose subtree.
    assert "component" not in s["labels"]


def test_same_name_different_metric_two_rules(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: high, metric: node_cpu, op: ">", window: 5m, threshold: "80:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: high, metric: container_cpu, op: ">", window: 5m, threshold: "80:warning"}\n',
    })
    pack = cc.build_pack(tmp_path)
    assert pack["_meta"]["shapes"] == 2                 # different metric → distinct rules


# --- 3. severity union (no forced mirror) ----------------------------------
def test_severity_union_emits_declared_branches_only(tmp_path):
    # one tenant warning, another tenant critical, SAME shape → both branches
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: w, metric: m, op: ">", window: 5m, threshold: "10:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: c, metric: m, op: ">", window: 5m, threshold: "20:critical"}\n',
    })
    shapes, _ = ld.build_shapes(tmp_path)
    assert len(shapes) == 1
    assert shapes[0]["severities"] == ["critical", "warning"]


def test_single_severity_no_critical_mirror(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: w, metric: m, op: ">", window: 5m, threshold: "10:warning"}\n',
    })
    shapes, _ = ld.build_shapes(tmp_path)
    assert shapes[0]["severities"] == ["warning"]       # NOT [critical, warning]


# --- 4. injection defence --------------------------------------------------
@pytest.mark.parametrize("bad", [
    "node_cpu} or vector(1)",        # break out of the matcher
    "tenant:alert_threshold:x",      # recording-rule reference (colon)
    "foo{bar=1}",                    # inline selector
    "a b",                           # whitespace
])
def test_metric_injection_rejected(bad):
    with pytest.raises(shp.RecipeError):
        shp.recipe_id({"recipe": "threshold", "metric": bad, "op": ">", "window": "5m"})


@pytest.mark.parametrize("label", ["tenant", "version", "severity", "__name__", "recipe_id", "name"])
def test_reserved_selector_label_rejected(label):
    with pytest.raises(shp.RecipeError):
        shp.assemble_selector({"recipe": "rate", "metric": "m", "selectors": {label: "x"}})


@pytest.mark.parametrize("bad_for", ["2m", "90s", "1.5h", "5min"])
def test_recipe_id_rejects_non_enum_for(bad_for):
    # TRK-326: `for` enters the recipe_id slug + shape_signature → a non-enum
    # value must fail loud at compile time (not silently mint a bogus shape).
    with pytest.raises(shp.RecipeError, match="for"):
        shp.recipe_id({"recipe": "threshold", "metric": "m", "op": ">", "window": "5m", "for": bad_for})


@pytest.mark.parametrize("falsy", [None, ""])
def test_recipe_id_for_falsy_defaults_to_1m(falsy):
    # falsy `for` (missing / null / empty) → "1m", matching custom_alert.go's
    # `if forVal == "" { forVal = "1m" }` so Go/Python never diverge on this case.
    rid = shp.recipe_id({"recipe": "threshold", "metric": "m", "op": ">", "window": "5m", "for": falsy})
    assert rid.endswith("__for1m")


def test_selector_value_is_escaped():
    sel = shp.assemble_selector({"recipe": "rate", "metric": "m",
                                 "selectors": {"path": 'a"b\\c'}})
    assert sel == '{path="a\\"b\\\\c"}'                   # quote + backslash escaped


# --- 5. uniqueness ---------------------------------------------------------
def test_duplicate_name_per_tenant_rejected(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: dup, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
                  '      - {recipe: rate, name: dup, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    with pytest.raises(CustomAlertConfigError, match="duplicate custom-alert name"):
        ld.build_shapes(tmp_path)


def test_two_same_severity_same_shape_rejected(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: w1, metric: m, op: ">", window: 5m, threshold: "10:warning"}\n'
                  '      - {recipe: threshold, name: w2, metric: m, op: ">", window: 5m, threshold: "20:warning"}\n',
    })
    with pytest.raises(CustomAlertConfigError, match="same shape"):
        ld.build_shapes(tmp_path)


def test_for_divergence_produces_distinct_shapes(tmp_path):
    # TRK-326 regression: two tenants share recipe/metric/op/window but set a
    # DIFFERENT `for`. Pre-fix, `for` was absent from recipe_id/shape_signature
    # and build_shapes froze the FIRST-seen `for`, silently dropping the other's.
    # Now `for` is in the slug → two distinct shapes, each tenant keeps its `for`.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: fast, metric: m, op: ">", window: 5m, threshold: "1:warning", for: 1m}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: slow, metric: m, op: ">", window: 5m, threshold: "1:warning", for: 15m}\n',
    })
    shapes, _ = ld.build_shapes(tmp_path)
    rids = sorted(s["recipe_id"] for s in shapes)
    assert len(rids) == 2, f"expected 2 distinct shapes (different for), got {rids}"
    assert any(r.endswith("__for1m") for r in rids)
    assert any(r.endswith("__for15m") for r in rids)
    assert {s["for"] for s in shapes} == {"1m", "15m"}  # each rule keeps its own for


def test_same_for_still_vectorizes_one_shape(tmp_path):
    # O(M) preserved: two tenants with the SAME for + shape → still ONE rule
    # (enum-bounding `for` caps the per-base-shape fan-out at a small constant).
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: x, metric: m, op: ">", window: 5m, threshold: "1:warning", for: 5m}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: y, metric: m, op: ">", window: 5m, threshold: "1:warning", for: 5m}\n',
    })
    shapes, _ = ld.build_shapes(tmp_path)
    assert len(shapes) == 1, "same for + shape must vectorize to ONE rule (O(M))"
    assert shapes[0]["recipe_id"].endswith("__for5m")


# --- 6. scope inheritance + cap count --------------------------------------
def test_domain_and_platform_inheritance(tmp_path):
    _write_tree(tmp_path, {
        "_defaults.yaml": "_custom_alerts:\n"
            '  - {recipe: absence, name: hb, metric: heartbeat_total, window: 10m, threshold: "0:critical"}\n',
        "shop.yaml": 'tenants:\n  shop-a:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: q, metric: qd, op: ">", window: 5m, threshold: "1:warning"}\n',
        "fin/_defaults.yaml": "_custom_alerts:\n"
            '  - {recipe: ratio, name: pf, metric: pf_total, denominator_metric: pa_total, op: ">", window: 5m, threshold: "0.01:critical"}\n',
        "fin/pay.yaml": "tenants:\n  pay-a: {}\n",
    })
    shapes, per_tenant = ld.build_shapes(tmp_path)
    # shop-a: platform absence + own threshold = 2; pay-a: platform absence + fin ratio = 2
    assert per_tenant == {"shop-a": 2, "pay-a": 2}
    # absence shape is shared by both tenants → still ONE absence rule
    rids = {s["recipe_id"] for s in shapes}
    assert sum(r.startswith("absence__") for r in rids) == 1


# --- 7. example fixture + --check ------------------------------------------
def test_example_fixture_compiles_to_eleven_shapes():
    pack = cc.build_pack(_EXAMPLES)
    assert pack["_meta"]["shapes"] == 11
    # shop-a: 9 own (threshold/rate/ratio/p99/absence/forecast + equals #810
    # + Shape-X ==/absence liveness pair #832); pay-a: finance ratio + own threshold.
    # (absence moved off platform-L0 → no longer inherited by pay-a; see _defaults.yaml note.)
    assert pack["_meta"]["per_tenant_counts"] == {"pay-a": 2, "shop-a": 9}


def test_check_flags_stale(tmp_path, monkeypatch):
    out = tmp_path / "rule-pack-custom-alerts.yaml"
    out.write_text("groups: []\n", encoding="utf-8")  # stale (empty)
    # drive via argv
    monkeypatch.setattr(sys, "argv", [
        "compile", "--check", "--config-dir", str(_EXAMPLES), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION


def test_write_out_outside_repo_does_not_crash(tmp_path, monkeypatch):
    # Regression: the success line did `out_path.relative_to(repo)`, which raises
    # ValueError for an --out OUTSIDE the repo (a CI scratch dir, or a different
    # drive on Windows) — and it ran AFTER the file was written, so a successful
    # compile crashed with a traceback + nonzero exit (false failure). tmp_path is
    # outside the repo, so the WRITE path (no --check) must still return EXIT_OK.
    out = tmp_path / "rule-pack-custom-alerts.yaml"
    monkeypatch.setattr(sys, "argv", [
        "compile", "--config-dir", str(_EXAMPLES), "--out", str(out)])
    assert cc.main() == cc.EXIT_OK
    assert out.exists() and "groups:" in out.read_text(encoding="utf-8")


# --- 8. forecast recipe (ADR-024 §Forecast Recipe, #741) -------------------
def test_forecast_ratio_mode_slug_and_records(tmp_path):
    # ratio mode: capacity_metric set → headroom ratio avail/capacity; horizon
    # (not window) enters the slug; lookback is platform-derived = max(2·4h,1h)
    # = 8h = 28800s; cold-start gate `> 3`; horizon 4h = 14400s.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: disk, metric: avail, capacity_metric: cap, op: "<", horizon: 4h, threshold: "0.15:warning"}\n',
    })
    txt = cc._render(cc.build_pack(tmp_path)["groups"])
    rid = "forecast__avail__lt__h4h__den_cap__for1m"
    assert rid in txt
    assert "sum by(tenant) (avail)" in txt and "sum by(tenant) (cap) > 0" in txt
    # W1: ratio-mode forecast clamps the (non-negative) predicted ratio and gates on
    # a current-state sanity floor (anti transient-write-burst FP); the tenant's own
    # threshold is unchanged (compared in the core).
    assert f"clamp_min(predict_linear(custom:fcbase:{rid}[28800s], 14400), 0)" in txt
    assert f"custom:fcbase:{rid} < 0.5" in txt
    assert f"count_over_time(custom:fcbase:{rid}[28800s]) > 3" in txt


def test_forecast_raw_mode_no_capacity(tmp_path):
    # raw mode: no capacity_metric → predict the gauge itself (max by tenant,version);
    # no den_ part; lookback 2·12h = 24h = 86400s, horizon 12h = 43200s.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: q, metric: queue_depth, op: ">", horizon: 12h, threshold: "10000:warning"}\n',
    })
    txt = cc._render(cc.build_pack(tmp_path)["groups"])
    assert "forecast__queue_depth__gt__h12h__for1m" in txt
    assert "den_" not in txt
    assert "max by(tenant, version) (queue_depth)" in txt
    assert "[86400s], 43200)" in txt
    # W1: raw mode (arbitrary gauge — may exceed 1 or go legitimately negative) gets
    # NEITHER the ratio clamp NOR the [0,1] current-state band (those are ratio-mode only).
    assert "clamp_min" not in txt
    assert "< 0.5" not in txt


def test_forecast_requires_horizon(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: q, metric: m, op: ">", threshold: "1:warning"}\n',
    })
    with pytest.raises(ld.CustomAlertConfigError, match="horizon"):
        ld.build_shapes(tmp_path)


@pytest.mark.parametrize("bad", ["3h", "90m", "5h", "8h"])
def test_forecast_horizon_enum_rejected(bad):
    with pytest.raises(shp.RecipeError, match="horizon"):
        shp.recipe_id({"recipe": "forecast", "metric": "m", "op": "<", "horizon": bad})


def test_forecast_ratio_threshold_at_or_above_band_rejected(tmp_path):
    # W1 footgun guard: a ratio-mode forecast floor >= the current-state band (0.5)
    # is silently neutered by `custom:fcbase < band`, so it is rejected loudly at load
    # (shape.validate_forecast_ratio_threshold). 0.5 itself is rejected (>= band).
    for bad in ("0.6:warning", "0.5:warning"):
        _write_tree(tmp_path, {
            "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                f'      - {{recipe: forecast, name: d, metric: avail, capacity_metric: cap, op: "<", horizon: 4h, threshold: "{bad}"}}\n',
        })
        with pytest.raises(ld.CustomAlertConfigError, match="current-state band"):
            ld.build_shapes(tmp_path)


def test_forecast_ratio_threshold_below_band_ok(tmp_path):
    # a sensible low disk-fill floor (< 0.5) loads fine.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: d, metric: avail, capacity_metric: cap, op: "<", horizon: 4h, threshold: "0.15:warning"}\n',
    })
    ld.build_shapes(tmp_path)  # no raise


def test_forecast_raw_mode_threshold_not_bounded_by_band(tmp_path):
    # raw mode (no capacity_metric) has NO band → a large absolute threshold (>= 0.5)
    # is fine; the band guard is ratio-mode only.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: q, metric: queue_depth, op: ">", horizon: 12h, threshold: "10000:warning"}\n',
    })
    ld.build_shapes(tmp_path)  # no raise (raw mode, band does not apply)


# --- 9. cost guardrail: max_custom_recipes per-tenant cap (S4) --------------
def test_own_recipe_cap_rejects_over_limit(tmp_path):
    # 3 OWN recipes, cap 2 → fail loud at compile (deterministic, actionable).
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: a, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: b, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: c, metric: m3, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    with pytest.raises(ld.CustomAlertConfigError, match="max_custom_recipes"):
        ld.build_shapes(tmp_path, max_custom_recipes=2)


def test_inherited_recipes_do_not_count_toward_cap(tmp_path):
    # domain _defaults has 2 policy recipes (inherited, vectorized); tenant has 1
    # OWN. effective = 3 but OWN = 1 ≤ cap 1 → OK (inherited is uncapped).
    _write_tree(tmp_path, {
        "dom/_defaults.yaml": "_custom_alerts:\n"
            '  - {recipe: threshold, name: p1, metric: pm1, op: ">", window: 5m, threshold: "1:warning"}\n'
            '  - {recipe: threshold, name: p2, metric: pm2, op: ">", window: 5m, threshold: "1:warning"}\n',
        "dom/t.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: own1, metric: om1, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    _shapes, per_tenant = ld.build_shapes(tmp_path, max_custom_recipes=1)  # no raise
    assert per_tenant["ta"] == 3   # effective = 2 inherited + 1 own (own ≤ cap)


def test_own_recipe_cap_at_limit_ok(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: a, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: b, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    _shapes, per_tenant = ld.build_shapes(tmp_path, max_custom_recipes=2)  # exactly at cap
    assert per_tenant["ta"] == 2


def test_build_pack_threads_cap(tmp_path):
    # CLI wiring guard: build_pack(max_custom_recipes=) must reach build_shapes.
    # the example fixture's shop-a has 9 OWN recipes → cap 5 must reject here.
    with pytest.raises(ld.CustomAlertConfigError, match="max_custom_recipes"):
        cc.build_pack(_EXAMPLES, max_custom_recipes=5)


def test_negative_cap_rejected(tmp_path):
    # a negative cap is nonsensical (CLI type=int lets it through) — fail loud
    # up front rather than reject every tenant with a confusing message. 0 is OK.
    _write_tree(tmp_path, {"a.yaml": "tenants:\n  ta: {}\n"})
    with pytest.raises(ld.CustomAlertConfigError, match=">= 0"):
        ld.build_shapes(tmp_path, max_custom_recipes=-1)


def test_own_duplicate_of_inherited_rejected_not_quota_charged(tmp_path):
    # phantom-quota guard: a tenant re-declaring a DOMAIN policy shape is REJECTED
    # (severity-uniqueness) BEFORE the quota counter — it never silently eats cap.
    _write_tree(tmp_path, {
        "dom/_defaults.yaml": "_custom_alerts:\n"
            '  - {recipe: threshold, name: pol, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
        "dom/t.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: dup, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    with pytest.raises(ld.CustomAlertConfigError, match="same shape"):
        ld.build_shapes(tmp_path, max_custom_recipes=100)


def test_multi_severity_same_shape_counts_as_two(tmp_path):
    # warning + critical of the SAME shape = 2 distinct alert rules → counts as 2
    # toward the cap (correct, not phantom — they ARE two rules).
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: w, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: c, metric: m, op: ">", window: 5m, threshold: "2:critical"}\n',
    })
    _shapes, per_tenant = ld.build_shapes(tmp_path, max_custom_recipes=100)
    assert per_tenant["ta"] == 2


# --- 10. cross-language validation contract (S5, ADR-024 §S5) ---------------
_VALIDATION_VECTORS = _REPO / "tests" / "dx" / "fixtures" / "custom_alert_validation_vectors.json"


def _py_validate_spec(spec: dict) -> bool:
    """Python's per-recipe accept/reject decision (the shared-contract subset:
    recipe/metric/op/horizon/selector-reserved/for via recipe_id + severity via
    parse_threshold). Mirrors the Go side's resolveOneCustomAlert for these rules."""
    try:
        shp.recipe_id(spec)
        shp.parse_threshold(spec["threshold"])
        return True
    except shp.RecipeError:
        return False


def test_validation_contract_matches_go():
    # Same fixture the Go test (TestValidationContract_GoldenVectors) asserts on:
    # Python and Go MUST agree on accept/reject, closing the validation-decision
    # drift the slug golden vectors didn't cover.
    cases = json.loads(_VALIDATION_VECTORS.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 8, "validation contract fixture undershot"
    for c in cases:
        accepted = _py_validate_spec(c["spec"])
        assert accepted == c["valid"], (
            f"validation drift [{c['_note']}]: Python accepted={accepted}, contract valid={c['valid']}"
        )


# --- D. disk-recipe prerequisite notice (#692 P0③ W3) ----------------------
# A recipe over kubelet_volume_stats_* compiles fine but only fires if the cluster
# has CSI NodeGetVolumeStats + a volume-stats scrape job + a namespace→tenant
# relabel — plumbing the compiler can't verify. main() surfaces it at author-time
# (honest: it INFORMS, does not assert). byo_check.py verifies the live flow.
def test_disk_recipe_emits_prereq_notice(tmp_path, capsys):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: disk_chk, metric: kubelet_volume_stats_available_bytes,'
                  ' op: ">", window: 5m, threshold: "1000000:warning"}\n',
    })
    # Real write path — #848 guards the success line against an out-of-repo --out, so
    # main() returns EXIT_OK on a tmp path; the notice fires regardless of compile mode.
    sys.argv = ["compile_custom_alerts.py", "--config-dir", str(tmp_path),
                "--out", str(tmp_path / "pack.yaml")]
    assert cc.main() == 0
    assert "disk-recipe prerequisite" in capsys.readouterr().err


def test_nondisk_recipe_no_prereq_notice(tmp_path, capsys):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">",'
                  ' window: 5m, threshold: "80:warning"}\n',
    })
    sys.argv = ["compile_custom_alerts.py", "--config-dir", str(tmp_path),
                "--out", str(tmp_path / "pack.yaml")]
    assert cc.main() == 0
    err = capsys.readouterr().err
    assert "disk-recipe prerequisite" not in err
    assert "disk-IOPS-recipe prerequisite" not in err


# --- D2. disk-IOPS-recipe prerequisite notice (#692 P0④) -------------------
# A rate recipe over container_fs_* compiles fine but only fires if cadvisor scrapes
# container_fs with a namespace→tenant relabel AND the storage exposes I/O to cgroup
# blkio (network volumes bypass it). main() surfaces it at author-time; byo_check is
# the live fidelity gate.
def test_iops_recipe_emits_prereq_notice(tmp_path, capsys):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: rate, name: iops_chk, metric: container_fs_writes_total,'
                  ' op: ">", window: 5m, threshold: "500:warning"}\n',
    })
    sys.argv = ["compile_custom_alerts.py", "--config-dir", str(tmp_path),
                "--out", str(tmp_path / "pack.yaml")]
    assert cc.main() == 0
    assert "disk-IOPS-recipe prerequisite" in capsys.readouterr().err
