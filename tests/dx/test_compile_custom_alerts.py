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
import re
import shlex
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
    # the sentinel carries component="sentinel" (NOT "custom"): it stays OUT of the
    # custom firehose subtree, and the static discriminator routes it into the
    # platform sentinel-sinkhole — never the tenant/NOC notification channels. It
    # carries a tenant label, so without the discriminator it would fall through to
    # the tenant main route and notify humans with severity=none noise (#1095).
    assert s["labels"]["component"] == "sentinel"


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
    shapes, _, _ = ld.build_shapes(tmp_path)
    assert len(shapes) == 1
    assert shapes[0]["severities"] == ["critical", "warning"]


def test_single_severity_no_critical_mirror(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: w, metric: m, op: ">", window: 5m, threshold: "10:warning"}\n',
    })
    shapes, _, _ = ld.build_shapes(tmp_path)
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
def test_duplicate_name_per_tenant_quarantined(tmp_path):
    # #1008 Part B: fail-soft — the second `dup` is QUARANTINED (recorded in `skipped`),
    # NOT a whole-compile abort; the first compiles.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: dup, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
                  '      - {recipe: rate, name: dup, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1                       # the first `dup` compiled
    assert any("duplicate custom-alert name" in s["reason"] for s in skipped)


def test_two_same_severity_same_shape_quarantined(tmp_path):
    # #1008 Part B: fail-soft — the second warning alert on the SAME shape is quarantined
    # (keeps the group_left(name) join 1:1); the first compiles.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: w1, metric: m, op: ">", window: 5m, threshold: "10:warning"}\n'
                  '      - {recipe: threshold, name: w2, metric: m, op: ">", window: 5m, threshold: "20:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1
    assert any("same shape" in s["reason"] for s in skipped)


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
    shapes, _, _ = ld.build_shapes(tmp_path)
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
    shapes, _, _ = ld.build_shapes(tmp_path)
    assert len(shapes) == 1, "same for + shape must vectorize to ONE rule (O(M))"
    assert shapes[0]["recipe_id"].endswith("__for5m")


# --- 5b. #1008 / F3: injective recipe_id + fail-soft quarantine -------------
def test_f3_lossy_selector_collision_resolved(tmp_path):
    # S1: selector values differing only in a sanitise-folded char ('us-east-1' vs
    # 'us_east_1') USED to collapse to one recipe_id → cross-tenant compile abort. The
    # injective __x{hash} suffix now yields TWO distinct shapes, no collision.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region: "us-east-1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region: "us_east_1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert skipped == []
    assert len({s["recipe_id"] for s in shapes}) == 2


def test_f3_separator_aliasing_collision_resolved(tmp_path):
    # S2: NON-lossy separator aliasing — {region_x:"1"} and {region:"x_1"} both flatten to
    # the slug part `s_region_x_1` with NO lossy char, yet are distinct shapes. The hash
    # is over the STRUCTURED signature (not the flat join), so they no longer collide.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region_x: "1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region: "x_1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert skipped == []
    assert len({s["recipe_id"] for s in shapes}) == 2


def test_f3_same_shape_still_vectorizes(tmp_path):
    # the injective suffix must NOT break vectorization: two tenants with the SAME
    # selector value still share one recipe_id → ONE rule (O(M) preserved).
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region: "us-east-1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: r, metric: m, selectors: {region: "us-east-1"}, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert skipped == []
    assert len(shapes) == 1


def test_failsoft_ratio_missing_denominator_quarantined(tmp_path):
    # F-A: a ratio recipe without denominator_metric used to raise an uncaught KeyError
    # (exit 1 = whole-platform DoS). It is now quarantined; a sibling valid recipe still
    # compiles.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: ratio, name: bad, metric: m, op: ">", window: 5m, threshold: "0.5:warning"}\n'
            '      - {recipe: threshold, name: ok, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1                            # the valid `ok` recipe compiled
    assert any(s["name"] == "bad" for s in skipped)


def test_failsoft_non_mapping_entry_quarantined(tmp_path):
    # F-B (loader level): a scalar list item is quarantined instead of crashing the
    # compile with an AttributeError; a sibling valid recipe still compiles.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - just_a_bare_string\n'
            '      - {recipe: threshold, name: ok, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1
    assert any("not a mapping" in s["reason"] for s in skipped)


def test_failsoft_malformed_yaml_file_quarantined(tmp_path):
    # #1008 Part B: a conf.d FILE that yaml.safe_load can't parse (a control char) is
    # quarantined at FILE level — the load happens OUTSIDE the per-recipe loop, so without
    # the file-level fail-soft one bad file (incl. a schema-check-skipped meta file) would
    # crash the whole compile. Sibling valid files still compile.
    _write_tree(tmp_path, {
        "bad.yaml": 'tenants:\n  "x\x1by":\n    _custom_alerts: []\n',   # \x1b → PyYAML ReaderError
        "good.yaml": 'tenants:\n  t:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: ok, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1                               # good.yaml compiled
    assert any(s["origin"] == "bad.yaml" for s in skipped)


def test_safe_log_strips_control_chars():
    # #1008: the quarantine CI-log line sanitizes control chars (newline / ANSI ESC / tab)
    # from tenant-controlled fields so a malformed value can't inject forged log lines or
    # terminal escapes.
    assert cc._safe_log("a\nFAKE\x1b[2Jb\tc") == "a?FAKE?[2Jb?c"


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
    shapes, per_tenant, _ = ld.build_shapes(tmp_path)
    # shop-a: platform absence + own threshold = 2; pay-a: platform absence + fin ratio = 2
    assert per_tenant == {"shop-a": 2, "pay-a": 2}
    # absence shape is shared by both tenants → still ONE absence rule
    rids = {s["recipe_id"] for s in shapes}
    assert sum(r.startswith("absence__") for r in rids) == 1


# --- 7. example fixture + --check ------------------------------------------
def test_example_fixture_compiles_to_twelve_shapes():
    pack = cc.build_pack(_EXAMPLES)
    assert pack["_meta"]["shapes"] == 12
    # shop-a: 10 own declarations (threshold/rate/ratio/p99/absence/forecast +
    # equals #810 + Shape-X ==/absence liveness pair #832 + slo_burn_rate ADR-031)
    # = 11 cap units (the slo declaration fans out to critical+warning → counts 2);
    # pay-a: finance ratio + own threshold.
    # (absence moved off platform-L0 → no longer inherited by pay-a; see _defaults.yaml note.)
    assert pack["_meta"]["per_tenant_counts"] == {"pay-a": 2, "shop-a": 11}


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


def test_forecast_requires_horizon_quarantined(tmp_path):
    # #1008 Part B: fail-soft — a forecast recipe missing `horizon` is quarantined, not a
    # whole-compile abort.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: forecast, name: q, metric: m, op: ">", threshold: "1:warning"}\n',
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert shapes == []
    assert any("horizon" in s["reason"] for s in skipped)


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
        shapes, _per, skipped = ld.build_shapes(tmp_path)   # #1008 Part B: fail-soft
        assert shapes == []
        assert any("current-state band" in s["reason"] for s in skipped)


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
def test_own_recipe_cap_quarantines_over_limit(tmp_path):
    # #1008 Part B: fail-soft — 3 OWN recipes with cap 2: the first 2 compile, the 3rd
    # (over cap) is quarantined DETERMINISTICALLY (triples in file+declaration order), not
    # a whole-compile abort.
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: a, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: b, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: c, metric: m3, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    shapes, per_tenant, skipped = ld.build_shapes(tmp_path, max_custom_recipes=2)
    assert per_tenant["ta"] == 2 and len(shapes) == 2   # first 2 compiled
    assert any("max_custom_recipes" in s["reason"] for s in skipped)


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
    _shapes, per_tenant, _ = ld.build_shapes(tmp_path, max_custom_recipes=1)  # no raise
    assert per_tenant["ta"] == 3   # effective = 2 inherited + 1 own (own ≤ cap)


def test_own_recipe_cap_at_limit_ok(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: a, metric: m1, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: b, metric: m2, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    _shapes, per_tenant, _ = ld.build_shapes(tmp_path, max_custom_recipes=2)  # exactly at cap
    assert per_tenant["ta"] == 2


def test_build_pack_threads_cap():
    # CLI wiring guard: build_pack(max_custom_recipes=) must reach build_shapes. The
    # example fixture's shop-a has 9 OWN recipes → cap 5 quarantines the excess (#1008
    # Part B fail-soft) and surfaces it in _meta["skipped"], so the pack still builds.
    pack = cc.build_pack(_EXAMPLES, max_custom_recipes=5)
    skipped = pack["_meta"]["skipped"]
    assert any("max_custom_recipes" in s["reason"] for s in skipped)


def test_negative_cap_rejected(tmp_path):
    # a negative cap is nonsensical (CLI type=int lets it through) — fail loud
    # up front rather than reject every tenant with a confusing message. 0 is OK.
    _write_tree(tmp_path, {"a.yaml": "tenants:\n  ta: {}\n"})
    with pytest.raises(ld.CustomAlertConfigError, match=">= 0"):
        ld.build_shapes(tmp_path, max_custom_recipes=-1)


def test_own_duplicate_of_inherited_quarantined_not_quota_charged(tmp_path):
    # phantom-quota guard, now fail-soft (#1008 Part B): a tenant re-declaring a DOMAIN
    # policy shape is QUARANTINED (severity-uniqueness) BEFORE the quota counter — the
    # inherited policy compiles, the duplicate is skipped and never eats cap.
    _write_tree(tmp_path, {
        "dom/_defaults.yaml": "_custom_alerts:\n"
            '  - {recipe: threshold, name: pol, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
        "dom/t.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: dup, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n',
    })
    _shapes, per_tenant, skipped = ld.build_shapes(tmp_path, max_custom_recipes=100)
    assert per_tenant["ta"] == 1                       # only the inherited policy counts
    assert any("same shape" in s["reason"] for s in skipped)


def test_multi_severity_same_shape_counts_as_two(tmp_path):
    # warning + critical of the SAME shape = 2 distinct alert rules → counts as 2
    # toward the cap (correct, not phantom — they ARE two rules).
    _write_tree(tmp_path, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
            '      - {recipe: threshold, name: w, metric: m, op: ">", window: 5m, threshold: "1:warning"}\n'
            '      - {recipe: threshold, name: c, metric: m, op: ">", window: 5m, threshold: "2:critical"}\n',
    })
    _shapes, per_tenant, _ = ld.build_shapes(tmp_path, max_custom_recipes=100)
    assert per_tenant["ta"] == 2


# --- 10. cross-language validation contract (S5, ADR-024 §S5) ---------------
_VALIDATION_VECTORS = _REPO / "tests" / "dx" / "fixtures" / "custom_alert_validation_vectors.json"


def _py_validate_spec(spec: dict) -> bool:
    """Python's per-recipe accept/reject decision (the shared-contract subset:
    recipe/metric/op/horizon/selector-reserved/for via recipe_id + severity via
    parse_threshold). Mirrors the Go side's resolveOneCustomAlert for these rules.
    slo_burn_rate (ADR-031) has NO threshold to parse — severity is fixed by the
    recipe; objective/slo_period/min_events (and the threshold-present rejection)
    are all validated inside recipe_id itself."""
    try:
        shp.recipe_id(spec)
        if spec.get("recipe") != "slo_burn_rate":
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


# --- 10b. slo_burn_rate recipe (ADR-031, #1092 Phase 0) ----------------------
_SLO_DECL = ('{recipe: slo_burn_rate, name: co_avail, metric: co_errors_total, '
             'denominator_metric: co_requests_total, objective: "99.9"}')


def test_slo_one_declaration_fans_out_both_severities(tmp_path):
    # severity is decided by the RECIPE (fast→critical, slow→warning): ONE
    # declaration → ONE shape carrying BOTH severity branches.
    _write_tree(tmp_path, {
        "a.yaml": f'tenants:\n  ta:\n    _custom_alerts:\n      - {_SLO_DECL}\n',
    })
    shapes, per_tenant, skipped = ld.build_shapes(tmp_path)
    assert skipped == []
    assert len(shapes) == 1
    assert shapes[0]["severities"] == ["critical", "warning"]
    assert shapes[0]["min_events"] == 10                    # default materialised
    # cap accounting: the declaration IS two alert rules → counts 2 (ADR-031 §guardrail 1)
    assert per_tenant == {"ta": 2}


def test_slo_counts_two_toward_own_cap(tmp_path):
    # cap 1 cannot fit an slo declaration (it needs 2 units) → quarantined
    # fail-soft; cap 2 fits exactly.
    files = {"a.yaml": f'tenants:\n  ta:\n    _custom_alerts:\n      - {_SLO_DECL}\n'}
    _write_tree(tmp_path, files)
    shapes, per_tenant, skipped = ld.build_shapes(tmp_path, max_custom_recipes=1)
    assert shapes == [] and per_tenant == {}
    assert any("max_custom_recipes" in s["reason"] for s in skipped)
    shapes, per_tenant, skipped = ld.build_shapes(tmp_path, max_custom_recipes=2)
    assert len(shapes) == 1 and per_tenant == {"ta": 2} and skipped == []


def test_slo_duplicate_same_shape_quarantined(tmp_path):
    # a second slo declaration on the SAME shape collides on both fixed
    # severities → quarantined (keeps the group_left(name) join 1:1).
    _write_tree(tmp_path, {
        "a.yaml": ('tenants:\n  ta:\n    _custom_alerts:\n'
                   f'      - {_SLO_DECL}\n'
                   '      - {recipe: slo_burn_rate, name: co_avail2, metric: co_errors_total, '
                   'denominator_metric: co_requests_total, objective: "99.5"}\n'),
    })
    shapes, _per, skipped = ld.build_shapes(tmp_path)
    assert len(shapes) == 1
    assert any("same shape" in s["reason"] for s in skipped)


@pytest.mark.parametrize("bad_obj", ["100", "0", "0.0", "100.0", "abc", "1e3", "-5", ""])
def test_slo_objective_rejected(bad_obj):
    # OPEN interval (0,100): =100 → budget 0 → always fires; =0 never fires;
    # non-decimal charset rejected (Go ParseFloat lockstep).
    with pytest.raises(shp.RecipeError, match="objective"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": bad_obj})


@pytest.mark.parametrize("good_obj", ["99.9", "99", "0.5", "99.999", "disable"])
def test_slo_objective_accepted(good_obj):
    rid = shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e",
                         "denominator_metric": "t", "objective": good_obj})
    # objective NEVER enters the slug — every accepted value yields the same rid
    assert rid == "slo_burn_rate__e__gt__den_t__minev10__for1m"


@pytest.mark.parametrize("bad_period", ["7d", "1w", "31d", "30"])
def test_slo_period_rejected(bad_period):
    with pytest.raises(shp.RecipeError, match="slo_period"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": "99.9", "slo_period": bad_period})


def test_slo_period_not_a_shape_component():
    # 28d vs 30d (and a different objective) → BYTE-IDENTICAL rid + equal
    # shape_signature: switching the budget period never re-slugs (ADR-031).
    a = {"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
         "objective": "99.9", "slo_period": "30d"}
    b = {"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
         "objective": "95", "slo_period": "28d"}
    assert shp.recipe_id(a) == shp.recipe_id(b)
    assert shp.shape_signature(a) == shp.shape_signature(b)


@pytest.mark.parametrize("bad_me", [0, -1, "10", 1.5, True, False])
def test_slo_min_events_rejected(bad_me):
    # positive YAML INTEGER only (bool is an int subclass — rejected explicitly;
    # a quoted string must not slug differently between Go and Python).
    with pytest.raises(shp.RecipeError, match="min_events"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": "99.9", "min_events": bad_me})


def test_slo_min_events_is_a_shape_component():
    base = {"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
            "objective": "99.9"}
    assert shp.recipe_id(base).endswith("__minev10__for1m")          # default materialised
    other = dict(base, min_events=25)
    assert shp.recipe_id(other).endswith("__minev25__for1m")
    assert shp.shape_signature(base) != shp.shape_signature(other)   # forks the shape


def test_slo_threshold_rejected():
    with pytest.raises(shp.RecipeError, match="objective"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": "99.9", "threshold": "0.01:critical"})


def test_slo_missing_denominator_rejected():
    with pytest.raises(shp.RecipeError, match="denominator_metric"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "objective": "99.9"})


@pytest.mark.parametrize("bad_op", ["<", "<=", ">=", "=="])
def test_slo_explicit_op_rejected(bad_op):
    with pytest.raises(shp.RecipeError):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": "99.9", "op": bad_op})


def test_slo_group_by_rejected():
    with pytest.raises(shp.RecipeError, match="group_by"):
        shp.recipe_id({"recipe": "slo_burn_rate", "metric": "e", "denominator_metric": "t",
                       "objective": "99.9", "group_by": ["persistentvolumeclaim"]})


def test_slo_rule_count_ledger(tmp_path):
    # ADR-031 §2 implementation checklist: the per-shape rule ledger. emit_shape
    # yields 9 recording (1 threshold + 4 SLI ratio windows + 2 bad-event windows
    # + 2 per-severity cores) + 2 alerts = 11; build_pack adds 1 custom_recipe_info
    # → 12 rules total for one slo shape (plus the global silent sentinel).
    _write_tree(tmp_path, {
        "a.yaml": f'tenants:\n  ta:\n    _custom_alerts:\n      - {_SLO_DECL}\n',
    })
    pack = cc.build_pack(tmp_path)
    rules = [r for g in pack["groups"] for r in g.get("rules", [])]
    recording = [r for r in rules if "record" in r and r["record"] != "custom_recipe_info"]
    info = [r for r in rules if r.get("record") == "custom_recipe_info"]
    alerts = [r for r in rules if r.get("alert", "").startswith("Custom_")]
    assert len(recording) == 9
    assert len(info) == 1
    assert len(alerts) == 2
    rid = "slo_burn_rate__co_errors_total__gt__den_co_requests_total__minev10__for1m"
    assert {r["record"] for r in recording} == {
        f"custom:threshold:{rid}",
        *{f"custom:metric:{rid}:{w}" for w in ("1h", "5m", "6h", "30m")},
        *{f"custom:bad:{rid}:{w}" for w in ("5m", "30m")},
        f"custom:{rid}:critical:core",
        f"custom:{rid}:warning:core",
    }


def test_slo_burn_multiplier_vectors_lockstep():
    # ADR-031 burn-threshold lockstep (Wave 2 companion): the Go exporter derives
    # user_threshold values as M × (1 − objective/100) with LOCKED multipliers
    # (fast = "1h burns 2%", slow = "6h burns 5%"; M = ratio × period ÷ window →
    # 30d: 14.4/6, 28d: 13.44/5.6). The shared fixture pins the resulting float64
    # BIT-IDENTICALLY on both sides (same IEEE-754 expression order); Go asserts
    # its resolveSloBurnRate output (TestSloBurnRate_MultiplierVectors), Python
    # re-computes here. The constants live in this TEST on purpose — the compiler
    # never derives thresholds (they are exporter/data-plane), so this is a pure
    # contract pin, not compiler logic.
    multipliers = {"30d": (14.4, 6.0), "28d": (13.44, 5.6)}
    fixture = _REPO / "tests" / "dx" / "fixtures" / "slo_burn_multiplier_vectors.json"
    vectors = json.loads(fixture.read_text(encoding="utf-8"))["vectors"]
    assert len(vectors) >= 4, "multiplier fixture undershot"
    assert {v["period"] for v in vectors} == set(multipliers), "both periods must be pinned"
    for v in vectors:
        fast, slow = multipliers[v["period"]]
        budget = 1 - float(v["objective"]) / 100
        assert v["thr_critical"] == fast * budget, (
            f"{v['period']}/{v['objective']}: thr_critical drifted from fast-M × budget"
        )
        assert v["thr_warning"] == slow * budget, (
            f"{v['period']}/{v['objective']}: thr_warning drifted from slow-M × budget"
        )


def test_slo_core_structure_and_measurement_never_suppressed(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": f'tenants:\n  ta:\n    _custom_alerts:\n      - {_SLO_DECL}\n',
    })
    pack = cc.build_pack(tmp_path)
    rid = "slo_burn_rate__co_errors_total__gt__den_co_requests_total__minev10__for1m"
    rules = {r["record"]: r["expr"] for g in pack["groups"]
             for r in g.get("rules", []) if "record" in r}
    fast = rules[f"custom:{rid}:critical:core"]
    slow = rules[f"custom:{rid}:warning:core"]
    # fast burn: 1h & 5m ratio windows + bad:5m > min_events(10); maintenance unless tail
    assert f"custom:metric:{rid}:1h" in fast and f"custom:metric:{rid}:5m" in fast
    assert f"(custom:bad:{rid}:5m > 10)" in fast
    assert fast.count("and on(tenant)") == 2
    assert 'user_state_filter{filter="maintenance"}' in fast
    # slow burn: 6h & 30m + bad:30m > min_events*6 (the ×6 linear window scaling
    # is compiler-side, ADR-031 §1)
    assert f"custom:metric:{rid}:6h" in slow and f"custom:metric:{rid}:30m" in slow
    assert f"(custom:bad:{rid}:30m > 60)" in slow
    # threshold join keeps the version exact-or-fallback + group_left(name, mode) idiom
    assert 'version="default", severity="critical"' in fast
    assert "group_left(name, mode)" in fast
    # measurement is NEVER suppressed: no maintenance unless on any SLI/bad record
    for name, expr in rules.items():
        if ":core" in name or name == "custom_recipe_info":
            continue
        assert "maintenance" not in expr, f"recording {name} must not be suppressed"


def test_slo_alert_labels_metric_group_and_slo_burn(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": f'tenants:\n  ta:\n    _custom_alerts:\n      - {_SLO_DECL}\n',
    })
    pack = cc.build_pack(tmp_path)
    alerts = [r for g in pack["groups"] for r in g.get("rules", [])
              if r.get("alert", "").startswith("Custom_")]
    assert len(alerts) == 2
    assert {a["labels"]["severity"] for a in alerts} == {"critical", "warning"}
    for a in alerts:
        # Severity Dedup inhibit needs metric_group on BOTH severities; slo_burn
        # is the storm-fan-out discriminator (ADR-031 §2, OQ-B de-prefixed).
        assert a["labels"]["metric_group"] == "slo_{{ $labels.name }}"
        assert a["labels"]["slo_burn"] == "true"
        assert a["labels"]["component"] == "custom"


def test_slo_selectors_apply_to_both_sides(tmp_path):
    _write_tree(tmp_path, {
        "a.yaml": ('tenants:\n  ta:\n    _custom_alerts:\n'
                   '      - {recipe: slo_burn_rate, name: co_avail, metric: co_errors_total, '
                   'denominator_metric: co_requests_total, objective: "99.9", '
                   'selectors: {service: checkout}}\n'),
    })
    pack = cc.build_pack(tmp_path)
    ratio_exprs = [r["expr"] for g in pack["groups"] for r in g.get("rules", [])
                   if r.get("record", "").startswith("custom:metric:")]
    assert len(ratio_exprs) == 4
    for expr in ratio_exprs:
        assert 'co_errors_total{service="checkout"}' in expr        # numerator
        assert 'co_requests_total{service="checkout"}' in expr      # denominator
        assert "> 0)" in expr                                        # div-by-zero guard


def test_slo_objective_disable_still_compiles(tmp_path):
    # tri-state opt-out is DATA-PLANE (the exporter never emits user_threshold);
    # the compiled rule set is unchanged — consistent with threshold "disable".
    _write_tree(tmp_path, {
        "a.yaml": ('tenants:\n  ta:\n    _custom_alerts:\n'
                   '      - {recipe: slo_burn_rate, name: co_avail, metric: co_errors_total, '
                   'denominator_metric: co_requests_total, objective: "disable"}\n'),
    })
    shapes, per_tenant, skipped = ld.build_shapes(tmp_path)
    assert skipped == []
    assert len(shapes) == 1 and per_tenant == {"ta": 2}


# --- 11. A+ emit-time invariant gate (F2 annotation-injection backstop) -------
def test_emit_invariant_gate_allows_platform_actions():
    """The emit-time invariant gate passes platform-authored template actions
    ({{ $value | printf }} / {{ $labels.X }}) — it must NOT false-positive."""
    cc._assert_annotations_template_safe([
        {"rules": [{
            "alert": "Custom_x",
            "labels": {"tenant": "{{ $labels.tenant }}", "severity": "warning",
                       "mode": "{{ $labels.mode }}", "recipe": "rate"},
            "annotations": {
                "summary": "Custom alert [{{ $labels.name }}] for {{ $labels.tenant }}",
                "description": 'rate on m{k="v"}: value {{ $value | printf "%.2f" }} crossed',
                "description_zh": '值 {{ $value | printf "%.0f" }} 等於設定代碼',
                "runbook_url": "{{ $labels.runbook_url }}"},
        }]},
    ])  # must not raise


@pytest.mark.parametrize("bad", [
    'absence on m{probe="{{ query `sum(x)` | first | value }}"}: v',   # F2 query() action
    'v {{ printf "x" }} done',                                          # non-allowlisted action
    "has a raw ` backtick",                                             # Go raw-string delimiter
])
def test_emit_invariant_gate_rejects_injection(bad):
    """Mutation-prove: a non-platform template action or backtick in ANY annotation
    fails the compile — the F2 backstop, whatever field carried it."""
    with pytest.raises(CustomAlertConfigError, match="invariant"):
        cc._assert_annotations_template_safe([
            {"rules": [{"alert": "Custom_x", "annotations": {"description": bad}}]},
        ])


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


# --- E. CLI default ↔ CI canonical source (drift gate) ----------------------
# The committed pack is compiled from the LIVE conf.d (#741 S3b), so a bare
# `--check` must scan that same tree — a diverged default makes every
# no-argument local run report phantom drift against the committed pack.
def test_cli_default_config_dir_matches_canonical_callers():
    canonical = "components/threshold-exporter/config/conf.d"
    assert cc.DEFAULT_CONFIG_REL == canonical
    assert (_REPO / canonical).is_dir()
    # Every repo caller pins the canonical tree explicitly; if the tree ever
    # moves, this forces the CLI default to move in the same PR.
    #
    # ⛔ SHARES `_tool_invocations` WITH THE `--out` CALLER TEST, and that is a fix,
    # not tidying. This assertion used to run its own line-anchored regex over the raw
    # file, which had both defects that regex shape has: a ⛔-comment mentioning
    # `--config-dir` (the kind this repo writes to warn about #1582) was read as a
    # caller and reddened the build, and a shell continuation hid a real flag.
    # Measured: adding one such comment to the Makefile turned this test red while the
    # invocations were untouched. Extracting from each file's grammar drops comments
    # by construction.
    for rel in ("Makefile", ".pre-commit-config.yaml", ".github/workflows/ci.yml"):
        scanned = {tok.split()[0] for inv in _tool_invocations(rel)
                   for tok in [inv.split("--config-dir", 1)[1].strip()]}
        assert scanned == {canonical}, f"{rel} scans {scanned}, CLI default is {canonical}"


# --- F. a compile that produces nothing must not erase a pack that has rules ---
# Measured chain this closes: rename ONE tenant file so the loader stops matching it
# (`db-b.yaml` -> `db-b.yml`, content byte-identical) -> `--check` goes red and told
# the operator to regenerate -> regenerating compiled 0 shapes and wrote them over the
# shipped pack (7016 -> 447 bytes) -> `--check` went GREEN again with every custom
# alert gone. Both halves are pinned here: the write refuses, and the drift message
# stops naming the destructive remedy.
_EMPTY_SOURCE = {"a.yaml": "tenants:\n  ta: {}\n"}
_ONE_RECIPE_SOURCE = {
    "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
              '      - {recipe: threshold, name: cpu_hot, metric: node_cpu, op: ">",'
              ' window: 5m, threshold: "80:warning"}\n',
}


def _compile_into(src: Path, out: Path, monkeypatch, *, extra=()) -> int:
    monkeypatch.setattr(sys, "argv",
                        ["compile", "--config-dir", str(src), "--out", str(out), *extra])
    return cc.main()


def test_predicate_only_fires_when_the_write_would_leave_nothing():
    # The predicate is the whole guard, so pin all four quadrants directly. Losing
    # SOME rules is ordinary work and must NOT fire — gating on any shrink would
    # fail-red on a tenant retiring one recipe, and the guard would get removed.
    # ⛔ Truthiness, not identity: `is False` would also fail for the equivalent
    # `return committed and not produced`, which returns the empty dict. That is a
    # refactor anyone may make and the annotation already promises a bool — pinning
    # the `bool()` call pins the implementation, not the behaviour.
    assert cc._erases_committed_rules({}, {"a": "1"})
    assert not cc._erases_committed_rules({}, {})                 # nothing to protect
    assert not cc._erases_committed_rules({"a": "1"}, {"a": "1"})
    assert not cc._erases_committed_rules({"a": "1"}, {"a": "1", "b": "2"})  # partial loss

    # The disclosure quantity is the same subtraction the message reports.
    assert cc._rules_regenerating_would_drop({}, {"a": "1"}) == {"a"}
    assert cc._rules_regenerating_would_drop({"a": "1"}, {"a": "1", "b": "2"}) == {"b"}
    assert cc._rules_regenerating_would_drop({"a": "1", "b": "2"}, {"a": "1"}) == set()


def test_write_refuses_to_erase_and_leaves_the_pack_byte_identical(tmp_path, monkeypatch):
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    before = out.read_bytes()
    assert cc._committed_rules(out), "fixture precondition: the pack must have rules to protect"

    _write_tree(src, _EMPTY_SOURCE)
    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_VIOLATION
    assert out.read_bytes() == before, "refusal must not touch the file at all"


def test_allow_empty_is_the_way_through(tmp_path, monkeypatch, capsys):
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch, extra=("--allow-empty",)) == cc.EXIT_OK
    assert cc._committed_rules(out) == {}


def test_empty_source_writes_freely_when_there_is_nothing_to_protect(tmp_path, monkeypatch):
    # Two shapes that must stay writable, or a first compile / an already-empty pack
    # would need a flag nobody would know to pass.
    src = tmp_path / "src"
    _write_tree(src, _EMPTY_SOURCE)

    absent = tmp_path / "absent.yaml"
    assert _compile_into(src, absent, monkeypatch) == cc.EXIT_OK
    assert absent.exists()

    empty = tmp_path / "empty.yaml"
    empty.write_text("groups: []\n", encoding="utf-8")
    assert _compile_into(src, empty, monkeypatch) == cc.EXIT_OK


def test_unreadable_committed_pack_does_not_block_the_repair(tmp_path, monkeypatch):
    # A pack that cannot be parsed has no content to protect, and regenerating it IS
    # the repair. Raising here would turn "your pack is corrupt" into "the compiler
    # no longer runs" — taking away the only way out.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    out.write_text("groups: [ this is not: valid: yaml\n", encoding="utf-8")
    assert cc._committed_rules(out) == {}
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    assert cc._committed_rules(out), "the repair must leave a readable pack"


def test_check_does_not_name_the_destructive_remedy_when_source_compiles_to_nothing(
        tmp_path, monkeypatch, capsys):
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compile", "--check",
                                      "--config-dir", str(src), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "make custom-alerts-compile" not in err
    assert cc.DO_NOT_REGENERATE in err
    # ⛔ Assert the diagnostic the reader NEEDS, not the wording it happens to use.
    # Pinning a phrase makes a rewrite — or the house rule that user-facing prose is
    # Traditional Chinese — into a red build for no behavioural reason.
    assert str(src) in err, "the message must say which tree the compiler read"
    assert "--allow-empty" in err, "the legitimate way through must be reachable from here"


def test_check_still_names_the_remedy_on_ordinary_drift(tmp_path, monkeypatch, capsys):
    # Positive control for the test above: same gate, same red, and the ONLY thing
    # that changed is whether the source still compiles to something. Without this,
    # deleting the remedy line altogether would keep that test green.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    out.write_text("groups: []\n", encoding="utf-8")  # stale pack, source unchanged
    monkeypatch.setattr(sys, "argv", ["compile", "--check",
                                      "--config-dir", str(src), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "make custom-alerts-compile" in err
    assert "--allow-empty" not in err
    assert cc.DO_NOT_REGENERATE, "an empty constant would make the next line vacuous"
    assert cc.DO_NOT_REGENERATE not in err


# --- G. the three shapes the erase guard does NOT stop, and what is said instead ---
def test_partial_loss_names_the_rules_regenerating_would_remove(tmp_path, monkeypatch, capsys):
    # Measured on the live tree: three tenants, rename one file, and this is the
    # branch you land in — rc=1 with "just regenerate", and following it drops that
    # tenant's alerts from the pack, the ConfigMaps and the CRD, every gate green.
    # The write is still allowed (retiring one recipe is ordinary work); what must
    # not happen is being told to regenerate without being told what that removes.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, {
        "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: a_hot, metric: a_m, op: ">",'
                  ' window: 5m, threshold: "80:warning"}\n',
        "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
                  '      - {recipe: threshold, name: b_hot, metric: b_m, op: ">",'
                  ' window: 5m, threshold: "80:warning"}\n',
    })
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "b.yaml").rename(src / "b.yml")   # byte-identical, just invisible now
    monkeypatch.setattr(sys, "argv", ["compile", "--check",
                                      "--config-dir", str(src), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "make custom-alerts-compile" in err       # still the right remedy here
    # …but not without the casualties. Rule identities are recipe_id slugs, so the
    # metric is what appears in them — NOT the tenant's `name:` for the recipe.
    assert "−" in err and "b_m" in err
    assert "net loss of coverage" in err, "nothing replaced the dropped rules; say so"

    # Positive control: drift with NOTHING dropped must not grow a casualty list.
    capsys.readouterr()
    (src / "b.yml").rename(src / "b.yaml")
    (src / "c.yaml").write_text(
        'tenants:\n  tc:\n    _custom_alerts:\n'
        '      - {recipe: threshold, name: c_hot, metric: c_m, op: ">",'
        ' window: 5m, threshold: "80:warning"}\n', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compile", "--check",
                                      "--config-dir", str(src), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "net loss of coverage" not in err
    assert "−custom-alerts" not in err


def test_an_empty_compile_never_reports_success_with_a_tick(tmp_path, monkeypatch, capsys):
    # The guard cannot fire when there is no pack to compare against, so a greenfield
    # tree whose declarations the loader cannot see compiles to nothing on day one and
    # every downstream gate agrees. `✅ Compiled 0 shape(s)` + rc=0 is the exact
    # signature of the incident this whole change exists for.
    src, out = tmp_path / "src", tmp_path / "absent.yaml"
    _write_tree(src, _EMPTY_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    cap = capsys.readouterr()
    assert "✅" not in cap.out
    assert "0 shape(s)" in cap.out and str(src) in cap.err

    # Positive control: a compile that DID produce rules still gets its tick.
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    assert "✅" in capsys.readouterr().out


def test_quarantine_is_diagnosed_as_itself_and_the_flag_is_refused(tmp_path, monkeypatch, capsys):
    # #1008 fail-soft drops an invalid recipe and compiles the rest, so one mistyped
    # `window:` in the only tenant that declares anything empties `produced` while the
    # source still declares it. Answering that with "--allow-empty" would delete every
    # committed rule to work around a one-character typo.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    before = out.read_bytes()
    capsys.readouterr()

    (src / "a.yaml").write_text(
        _ONE_RECIPE_SOURCE["a.yaml"].replace("window: 5m", "window: 5x"), encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "QUARANTINED" in err
    assert "renamed" not in err, "a quarantine must not send the reader hunting for a rename"
    # ⛔ The first version of this fix told the reader NOT to use --allow-empty and
    # then printed a runnable command containing it three lines down — the defect
    # this whole change is about, reproduced inside the fix for it. Assert the
    # command is absent, not merely that a warning is present.
    assert "compile_custom_alerts.py" not in err
    # …and the same must hold for the --check side, which shares the offer helper.
    monkeypatch.setattr(sys, "argv", ["compile", "--check",
                                      "--config-dir", str(src), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
    check_err = capsys.readouterr().err
    assert "QUARANTINED" in check_err
    assert "compile_custom_alerts.py" not in check_err

    # …and the flag itself is refused in this state, not merely discouraged.
    assert _compile_into(src, out, monkeypatch, extra=("--allow-empty",)) == cc.EXIT_VIOLATION
    assert out.read_bytes() == before

    # Positive control: with the typo fixed, the same flag writes as before.
    capsys.readouterr()
    _write_tree(src, _EMPTY_SOURCE)
    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch, extra=("--allow-empty",)) == cc.EXIT_OK


def test_refusal_prints_a_command_the_reader_can_actually_run(tmp_path, monkeypatch, capsys):
    # The documented entry point is `make custom-alerts-compile`, whose recipe takes
    # no arguments — so naming a bare flag leaves the reader to reconstruct the whole
    # invocation, or to hard-wire the flag into the Makefile and disarm the guard.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert str(src) in err and str(out) in err
    assert "--allow-empty" in err


def test_a_missing_config_dir_is_a_caller_error_not_a_success(tmp_path, monkeypatch, capsys):
    # Pre-existing branch, unpinned until now: `main()` grew a second non-zero exit
    # in this change and only that one was asserted, which reads as if the exit-code
    # contract were covered. Measured: returning EXIT_OK here survived the suite.
    monkeypatch.setattr(sys, "argv", ["compile", "--config-dir", str(tmp_path / "nope"),
                                      "--out", str(tmp_path / "pack.yaml")])
    assert cc.main() == cc.EXIT_CALLER_ERROR
    assert "config dir not found" in capsys.readouterr().err
    assert not (tmp_path / "pack.yaml").exists()


# --- H. the message must stay true when two causes are true at once ------------
_TWO_TENANTS = {
    "a.yaml": 'tenants:\n  ta:\n    _custom_alerts:\n'
              '      - {recipe: threshold, name: a_hot, metric: a_m, op: ">",'
              ' window: 5m, threshold: "80:warning"}\n',
    "b.yaml": 'tenants:\n  tb:\n    _custom_alerts:\n'
              '      - {recipe: threshold, name: b_hot, metric: b_m, op: ">",'
              ' window: 5m, threshold: "80:warning"}\n',
}


def test_quarantine_never_swallows_which_tree_was_read(tmp_path, monkeypatch, capsys):
    # A rename and a typo can both be true. The first version of the quarantine note
    # early-returned and took "The compiler read: <dir>" with it — a line the version
    # BEFORE it printed unconditionally, so that fix was a net loss of disclosure.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _TWO_TENANTS)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").rename(src / "a.yml")                        # invisible
    (src / "b.yaml").write_text(                                   # …and quarantined
        _TWO_TENANTS["b.yaml"].replace("window: 5m", "window: 5x"), encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    assert "QUARANTINED" in err
    assert str(src) in err, "which tree was read must survive the quarantine branch"


def test_an_empty_compile_blames_quarantine_when_that_is_what_happened(
        tmp_path, monkeypatch, capsys):
    # No pack on disk means the erase guard has no baseline, so this warning is the
    # only thing said at all — and "the compiler did not see their declarations" is
    # false when the compiler saw them and threw them out itself.
    src, out = tmp_path / "src", tmp_path / "greenfield.yaml"
    _write_tree(src, {
        "a.yaml": _ONE_RECIPE_SOURCE["a.yaml"].replace("window: 5m", "window: 5x")})
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    assert "were QUARANTINED above" in capsys.readouterr().err

    # Positive control: a genuinely empty source gets the discovery checklist instead.
    _write_tree(src, _EMPTY_SOURCE)
    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    err2 = capsys.readouterr().err
    assert "were QUARANTINED above" not in err2
    assert "Check, in this order" in err2


def test_the_write_that_drops_rules_says_so_too(tmp_path, monkeypatch, capsys):
    # The casualty note used to live only on --check, i.e. on the branch that changes
    # nothing, while the write that actually removed the rules printed a tick.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _TWO_TENANTS)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "b.yaml").rename(src / "b.yml")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK    # still allowed
    cap = capsys.readouterr()
    assert "✅" in cap.out                                        # …it did compile something
    assert "net loss of coverage" in cap.err and "b_m" in cap.err

    # Positive control: a compile that drops nothing stays quiet.
    capsys.readouterr()
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    assert "net loss of coverage" not in capsys.readouterr().err


def test_the_printed_command_survives_a_path_with_a_space(tmp_path, monkeypatch, capsys):
    # Measured on an unquoted version: argparse answered `unrecognized arguments:
    # conf.d` (rc=2) for a --config-dir under "…/my conf.d". `C:/Users/First Last/…`
    # and `OneDrive - Company/…` are ordinary on the hosts this runs on.
    src, out = tmp_path / "my src", tmp_path / "my pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_VIOLATION
    offer = [ln for ln in capsys.readouterr().err.splitlines()
             if "--allow-empty" in ln and "Do NOT" not in ln]
    assert len(offer) == 1, "exactly one paste-able command"
    # ⛔ posix=True, and no `.strip('"')`. This is the parser a pasted line meets,
    # and stripping quotes by hand hides the failure it exists to catch: the earlier
    # whitespace-only quoting produced a line this same call REFUSES to tokenize.
    argv = shlex.split(offer[0].strip(), posix=True)
    assert argv[argv.index("--config-dir") + 1] == str(src)
    assert argv[argv.index("--out") + 1] == str(out)
    # ⛔ …and it must invoke THIS interpreter, not the word "python": on a stock
    # Windows host that resolves to the Microsoft Store stub (exit 49, no output),
    # and every caller in this repo spells it "python3".
    assert argv[0] == sys.executable
    assert argv[1] == str(Path(cc.__file__).resolve())


@pytest.mark.parametrize("value", ["plain", "a b", 'a"b', "a'b", "a$b"])
def test_quoting_round_trips_through_the_parser_a_pasted_line_meets(value):
    # Measured, not asserted by eye: the whitespace-only rule this replaced round-
    # tripped three of these five and raised ValueError on the two carrying a quote
    # character — it emitted a command line that does not parse. (CodeRabbit, #1591.)
    #
    # ⚠️ `shlex.split` tokenizes but does not EXPAND, so this cannot demonstrate the
    # `a$b` case, where a double-quoted value would also be substituted by sh. That
    # half stays a prediction; driving a real shell from the dev host produced two
    # broken harnesses in a row (a must-fire control came back rc=127).
    assert shlex.split(cc._shell_quote(value), posix=True) == [value]


# --- I. one mechanical sweep over the whole message space ----------------------
# ⛔ WHY A SWEEP AND NOT ANOTHER CASE. Every defect this guard shipped was the same
# species: ONE branch's message either contradicted itself (forbidding a flag and
# then printing a command containing it) or blamed the wrong cause. Three review
# rounds each caught one instance and each fix created the next, because a per-case
# assertion only ever looks at the branch someone thought of. These invariants hold
# for every branch by construction, so a new branch cannot quietly opt out.
_TYPO_SOURCE = {"a.yaml": _ONE_RECIPE_SOURCE["a.yaml"].replace("window: 5m", "window: 5x")}

# ⛔ THE SEED SOURCE IS A COLUMN BECAUSE IT HAD TO BE. With the seed hard-wired
# to `_ONE_RECIPE_SOURCE`, the row labelled `partial-loss-check` was byte-for-byte
# identical to `quarantined-vs-pack-check` in every field but its name: it seeded
# one rule and then presented a source that compiles to zero, which is total loss.
# The sweep advertised eight states and exercised seven, and the missing one was
# the state the label named. (CodeRabbit, #1591.) Partial loss needs a seed with
# TWO tenants and a follow-up that presents only one of them.
_STATES = [
    # (label, seed source, source files, seed the pack first?, extra flags, --check?)
    ("empty-source-vs-pack", _ONE_RECIPE_SOURCE, _EMPTY_SOURCE, True, (), False),
    ("empty-source-vs-pack-check", _ONE_RECIPE_SOURCE, _EMPTY_SOURCE, True, (), True),
    ("quarantined-vs-pack", _ONE_RECIPE_SOURCE, None, True, (), False),
    ("quarantined-vs-pack-check", _ONE_RECIPE_SOURCE, None, True, (), True),
    ("quarantined-plus-allow-empty", _ONE_RECIPE_SOURCE, None, True, ("--allow-empty",), False),
    ("greenfield-empty", _ONE_RECIPE_SOURCE, _EMPTY_SOURCE, False, (), False),
    ("greenfield-quarantined", _ONE_RECIPE_SOURCE, None, False, (), False),
    ("partial-loss-check", _TWO_TENANTS, {"a.yaml": _TWO_TENANTS["a.yaml"]}, True, (), True),
    ("partial-loss-write", _TWO_TENANTS, {"a.yaml": _TWO_TENANTS["a.yaml"]}, True, (), False),
]


def test_the_sweep_states_are_all_distinct():
    # The sweep's value is the number of DIFFERENT states it drives main() through.
    # Two rows that differ only in their label make the count a claim rather than a
    # measurement — which is what happened, and the duplicate row was the one whose
    # name promised the state nobody was testing.
    keys = [(id(seed), id(files), pre, extra, chk) for _, seed, files, pre, extra, chk in _STATES]
    dupes = {k for k in keys if keys.count(k) > 1}
    assert not dupes, f"{len(dupes)} sweep state(s) appear twice under different labels"
    assert len(keys) == len(_STATES)


@pytest.mark.parametrize("label,seed_files,files,seed,extra,check", _STATES)
def test_no_message_forbids_and_offers_the_same_thing(
        label, seed_files, files, seed, extra, check, tmp_path, monkeypatch, capsys):
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    if seed:
        _write_tree(src, seed_files)
        assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
        capsys.readouterr()
    present = files or _TYPO_SOURCE
    # ⛔ Remove what the seed left behind. `_write_tree` only overwrites the files it
    # is handed, so without this the seed's `b.yaml` stays visible and the row that
    # exists to drop a tenant drops nothing. (CodeRabbit, #1591.)
    for stale in list(src.glob("*.yaml")) + list(src.glob("*.yml")):
        if stale.name not in present:
            stale.unlink()
    _write_tree(src, present)

    argv = ["compile", "--config-dir", str(src), "--out", str(out), *extra]
    if check:
        argv.insert(1, "--check")
    monkeypatch.setattr(sys, "argv", argv)
    cc.main()
    cap = capsys.readouterr()
    _assert_message_invariants(cap.out + cap.err, str(src), label)


def _assert_message_invariants(text: str, tree: str, label: str) -> None:
    """The invariants themselves, so the sweep and its control run the SAME code.

    ⛔ A sweep that only ever sees healthy input proves nothing — it is satisfied
    just as well by looking at less. `test_the_sweep_would_notice` feeds this the
    exact shapes the three review rounds actually shipped.
    """
    offers_command = Path(cc.__file__).name in text
    # 1. A prohibition and the thing it prohibits never appear together.
    if cc.DO_NOT_ALLOW_EMPTY in text:
        assert not offers_command, f"{label}: forbids --allow-empty and then hands it over"
    if cc.DO_NOT_REGENERATE in text:
        assert "make custom-alerts-compile" not in text, f"{label}: forbids and prescribes"
    # 2. Whenever the tool explains an empty compile, it says whose tree it read.
    if "Check, in this order" in text or "were QUARANTINED above" in text:
        assert tree in text, f"{label}: explained the emptiness without naming the tree"
    # 3. It never blames invisibility for recipes it quarantined itself.
    if "QUARANTINED (fail-soft" in text:
        assert "Check, in this order" not in text, f"{label}: quarantine blamed on discovery"


def test_the_sweep_would_notice():
    # Positive control: each of these is a shape that was really shipped and really
    # reviewed out. If the sweep stops failing on them it has stopped looking.
    assert cc.DO_NOT_ALLOW_EMPTY and cc.DO_NOT_REGENERATE, "empty constants make it vacuous"
    tool = Path(cc.__file__).name
    for label, text in [
        ("forbids-then-offers",
         cc.DO_NOT_ALLOW_EMPTY + "\n     " + tool + " --config-dir /t --allow-empty"),
        ("forbids-then-prescribes",
         cc.DO_NOT_REGENERATE + "\n   Run `make custom-alerts-compile` to regenerate."),
        ("explains-without-naming-the-tree",
         "   Check, in this order:\n     1. a declaration the compiler stopped seeing"),
        ("quarantine-blamed-on-discovery",
         "  QUARANTINED (fail-soft, #1008)\n   Check, in this order:\n/t"),
    ]:
        with pytest.raises(AssertionError):
            _assert_message_invariants(text, "/t", label)
    # …and it must pass on a healthy message, or it would "catch" everything.
    _assert_message_invariants("   The compiler read: /t\n   Check, in this order:\n", "/t", "ok")


# --- L. a write must name its target (#1582) --------------------------------
# Measured chain this closes, on `rule-packs/recipes/examples/conf.d` — a tree the
# module docstring says stays reachable via `--config-dir`, i.e. an invocation the
# tool itself advertises: compiling it with no `--out` wrote it over the SHIPPED
# pack (7016 -> 42302 bytes, 11 rules -> 65) at rc=0 — loud (the casualty note names
# all 8 removed rules) but advisory, which is why rc=0 is the problem. Running from
# outside the repo did not help — the default is `__file__`-anchored, so `cwd`
# isolation cannot reach it, which is why "just run it in a tmpdir" is not the fix.
def test_a_write_without_out_is_refused(tmp_path, monkeypatch, capsys):
    # ⛔ `_repo_root` IS REDIRECTED, AND THAT IS NOT COSMETIC. This is the one test
    # that exercises the unguarded path, so the default `out_path` it computes is the
    # SHIPPED pack. Measured: during mutation verification, the two mutants that
    # remove the guard turned this very test into a writer and it overwrote
    # `rule-packs/rule-pack-custom-alerts.yaml` with this fixture's single recipe —
    # the exact defect #1582 is about, produced by its own regression test. Pointing
    # the root at tmp keeps the blast radius inside tmp no matter what main() does.
    src, fake_repo = tmp_path / "src", tmp_path / "repo"
    (fake_repo / Path(cc.OUT_REL).parent).mkdir(parents=True)
    monkeypatch.setattr(cc, "_repo_root", lambda: fake_repo)
    _write_tree(src, _ONE_RECIPE_SOURCE)
    monkeypatch.setattr(sys, "argv", ["compile", "--config-dir", str(src)])

    assert cc.main() == cc.EXIT_CALLER_ERROR
    # ⛔ The load-bearing assertion is that NOTHING was written — a message check
    # alone is satisfied by a main() that prints the refusal and writes anyway.
    assert not (fake_repo / cc.OUT_REL).exists(), "a refused write must not write"
    err = capsys.readouterr().err
    assert cc.OUT_REQUIRED in err
    # It must also name the pack that was at risk; refusing without saying which file
    # the default would have hit leaves the reader unable to tell what was in danger.
    assert cc.OUT_REL in err


def test_the_same_source_writes_when_out_is_named(tmp_path, monkeypatch):
    # ⛔ MUST-FIRE CONTROL for the test above: it is what stops the gate from drifting
    # into "refuse writes generally".
    # ⚠️ An earlier version of this comment claimed the control was what catches an
    # unconditional `return EXIT_CALLER_ERROR` at the top of main(). Blind review
    # measured that: the refusal test catches that mutant on its own, at
    # `assert cc.OUT_REQUIRED in err` (stderr is empty). The control is still worth
    # keeping — it is the only thing asserting a named `--out` still WRITES — but not
    # for the reason that was written here.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    # ⛔ NOT `"cpu_hot" in out.read_text()`. ADR-024 rules are VECTORISED — one rule
    # per shape, with the recipe name arriving at runtime through `group_left` on
    # `user_threshold` — so the declaration's name is deliberately absent from the
    # pack. Asserting on it passes only by accident and fails for the right code.
    assert cc._committed_rules(out), "a named --out must actually receive rules"


def test_check_is_not_blocked_by_the_out_requirement(tmp_path, monkeypatch, capsys):
    # `--check` compares against the default pack and writes nothing, and all three
    # read-only callers in this repo omit `--out`. Both runs below return
    # EXIT_CALLER_ERROR, so the exit code cannot tell them apart — the discriminator
    # is WHICH refusal fired, which is what the named constant is for.
    missing = tmp_path / "no-such-tree"

    monkeypatch.setattr(sys, "argv", ["compile", "--check", "--config-dir", str(missing)])
    assert cc.main() == cc.EXIT_CALLER_ERROR
    assert cc.OUT_REQUIRED not in capsys.readouterr().err, "--check must reach the tree"

    # Same argv without `--check`: now the gate is the one that answers.
    monkeypatch.setattr(sys, "argv", ["compile", "--config-dir", str(missing)])
    assert cc.main() == cc.EXIT_CALLER_ERROR
    assert cc.OUT_REQUIRED in capsys.readouterr().err


class _Args:
    """Whatever `_rerun_command` reads. Built here so the three cases can be reached
    without a CLI run that would have to be steered past two other guards first."""
    def __init__(self, config_dir=None, out=None):
        self.config_dir = config_dir
        self.out = out
        self.max_custom_recipes = None  # set by the caller below


@pytest.mark.parametrize("config_dir,out,expect", [
    (None, "/tmp/mine.yaml", "echoes the reader's own target"),
    (None, None, "echoes the repo default"),
    ("/tmp/other-tree", None, "offers NOTHING"),
    ("/tmp/other-tree", "/tmp/mine.yaml", "echoes the reader's own target"),
])
def test_the_offer_echoes_a_target_and_never_invents_one(config_dir, out, expect):
    # ⛔ THE THIRD ROW IS THE ONE THAT WAS MEASURED DESTROYING THE SHIPPED PACK, and it
    # was destroyed by an earlier version of THIS change. That version filled the
    # missing `--out` in with the repo default "so the reader can see which pack they
    # are about to empty"; blind review pasted the resulting line verbatim and got
    # rc=0 with `rule-packs/rule-pack-custom-alerts.yaml` at 447 bytes, down from 7016.
    # Before that version the line carried no `--out` and the write gate refused it —
    # so naming the default converted a BLOCKED command into a working destructive one.
    #
    # Row 2 is not the same shape: with no `--config-dir` the pack `--check` compared
    # against IS the repo default, so echoing it is the legitimate "the last recipe
    # really was removed" flow, and row 2 is what keeps row 3's fix from being
    # "print nothing, ever".
    a = _Args(config_dir, out)
    a.max_custom_recipes = cc._loader.MAX_CUSTOM_RECIPES_DEFAULT
    cmd = cc._rerun_command(a)
    if expect == "offers NOTHING":
        assert cmd == "", "a redirected --config-dir with no --out must get no command"
        return
    argv = shlex.split(cmd, posix=True)
    assert "--allow-empty" in argv
    target = argv[argv.index("--out") + 1]
    assert target == (out if out else str(_REPO / cc.OUT_REL))
    if out:
        assert target != str(_REPO / cc.OUT_REL), "must not silently retarget the reader"


def test_a_withheld_offer_says_why_instead_of_going_quiet(tmp_path, monkeypatch, capsys):
    # End-to-end on the reachable path: `--check` against another tree with no `--out`.
    # ⛔ Two things must hold together — no paste-able command AND an explanation.
    # Returning "" from the offer builder without this branch would leave a message
    # that stops mid-sentence, which reads as a bug rather than a decision.
    src, out = tmp_path / "src", tmp_path / "pack.yaml"
    _write_tree(src, _ONE_RECIPE_SOURCE)
    assert _compile_into(src, out, monkeypatch) == cc.EXIT_OK
    capsys.readouterr()

    (src / "a.yaml").write_text(_EMPTY_SOURCE["a.yaml"], encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["compile", "--check", "--config-dir", str(src)])
    assert cc.main() == cc.EXIT_VIOLATION
    err = capsys.readouterr().err
    offer = [ln for ln in err.splitlines() if "--allow-empty" in ln and "Do NOT" not in ln]
    assert offer == [], "no paste-able command may be offered here"
    assert "--out" in err and "--config-dir" in err, "it must say what to do instead"


def _tool_invocations(rel: str) -> list[str]:
    """Executable invocations of the compiler in one caller file.

    ⛔ EXTRACTED FROM EACH FILE'S GRAMMAR, NOT BY LOOKING AT LINES. Two failures of
    the line-based version this replaces, both measured by blind review:

    * **Line-bounded.** `[^\\n]*` cannot see a flag on a shell continuation, so
      wrapping the (156-character) Makefile recipe made the invocation *look* like it
      had no `--out` — a red build with a message that was factually false — and,
      worse, made it invisible to the count, which let the real evasion through.
    * **Comment-blind only by luck.** The claim that anchoring on `--config-dir`
      excludes prose held only for prose that omits `--config-dir`; a ⛔-comment
      warning about this very hazard was misread as a writing invocation.

    So: fold shell continuations for the Makefile and take only TAB-indented recipe
    lines; for the YAML callers parse the document and walk string values, which drops
    comments by construction rather than by pattern.
    """
    text = (_REPO / rel).read_text(encoding="utf-8")
    if rel.endswith((".yaml", ".yml")):
        found: list[str] = []

        def walk(node):
            if isinstance(node, str):
                if "compile_custom_alerts.py" in node:
                    found.append(" ".join(node.split()))
            elif isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(yaml.safe_load(text))
        return found
    folded = text.replace("\\\n", " ")
    return [" ".join(ln.split()) for ln in folded.splitlines()
            if ln.startswith("\t") and "compile_custom_alerts.py" in ln]


def test_every_repo_caller_that_writes_names_its_out():
    # Derived, not enumerated: for EVERY executable invocation, either it is a
    # `--check` (writes nothing, `--out` optional) or it must carry `--out`. A new
    # writing caller that forgets it fails here rather than at rc=2 in someone's
    # terminal.
    expected = {"Makefile": 2, ".pre-commit-config.yaml": 1, ".github/workflows/ci.yml": 1}
    for rel, n in expected.items():
        calls = _tool_invocations(rel)
        # ⛔ EQUALITY, NOT A FLOOR. The floor this replaces was `total >= 3` while the
        # real total was 4 — one whole invocation of slack, and blind review used
        # exactly that slack: wrap the guarded Makefile recipe (hiding it from the
        # pattern) AND drop its `--out`, and the suite stayed green with the write
        # target unguarded. A count that can absorb a disappearance is not
        # anti-vacuity. Per file, because a global total lets one file's loss be
        # masked by another's gain.
        assert len(calls) == n, (
            f"{rel}: expected {n} invocation(s), extracted {len(calls)}: {calls}. "
            f"If a caller was deliberately added or removed, update this number.")
        for invocation in calls:
            assert "--check" in invocation or "--out" in invocation, (
                f"{rel}: a writing invocation with no --out will exit 2: {invocation}")


def test_the_gate_does_not_depend_on_config_dir(tmp_path, monkeypatch, capsys):
    """⛔ The central design decision, pinned — it was unpinned until blind review.

    Three places in this change argue the gate must be on `--out` and NOT on "did
    they redirect `--config-dir`". Nothing tested it: adding `and args.config_dir is
    not None` to the condition left all tests green, and the measured cost of that
    one clause is this pair — with it, the default-config-dir `--allow-empty` path
    writes the shipped pack; without it, that path is refused. The `--allow-empty`
    remedy is reachable from the DEFAULT config-dir, which is exactly why gating on
    the input cannot stand in for gating on the output.

    `_repo_root` is redirected for the same reason as in the refusal test: this is a
    write path with no `--out`, so an unguarded main() would target the real pack.
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / Path(cc.OUT_REL).parent).mkdir(parents=True)
    monkeypatch.setattr(cc, "_repo_root", lambda: fake_repo)

    # No --config-dir at all, and --allow-empty: the most "surely this one is fine"
    # invocation there is. It must still be refused, and refused for THIS reason —
    # the exit code alone cannot discriminate, because the config-dir check that a
    # gated-on-input variant would fall through to returns the same code. That is
    # what `OUT_REQUIRED` is named for.
    monkeypatch.setattr(sys, "argv", ["compile", "--allow-empty"])
    assert cc.main() == cc.EXIT_CALLER_ERROR
    assert not (fake_repo / cc.OUT_REL).exists()
    assert cc.OUT_REQUIRED in capsys.readouterr().err
