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
    assert len(_alert_names(pack)) == 1


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
def test_example_fixture_compiles_to_seven_shapes():
    pack = cc.build_pack(_EXAMPLES)
    assert pack["_meta"]["shapes"] == 7
    # shop-a: 5 own (threshold/rate/ratio/p99/absence); pay-a: finance ratio + own threshold.
    # (absence moved off platform-L0 → no longer inherited by pay-a; see _defaults.yaml note.)
    assert pack["_meta"]["per_tenant_counts"] == {"pay-a": 2, "shop-a": 5}


def test_check_flags_stale(tmp_path, monkeypatch):
    out = tmp_path / "rule-pack-custom-alerts.yaml"
    out.write_text("groups: []\n", encoding="utf-8")  # stale (empty)
    # drive via argv
    monkeypatch.setattr(sys, "argv", [
        "compile", "--check", "--config-dir", str(_EXAMPLES), "--out", str(out)])
    assert cc.main() == cc.EXIT_VIOLATION
