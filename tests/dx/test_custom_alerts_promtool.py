"""promtool behavioral golden tests for the Custom Alerts compiler (#741 S1+S2).

S1+S2 delivers the COMPILER, not a committed/deployed rule pack — the exporter
does not yet emit `user_threshold{recipe_id,name}` (that is S3), and the repo's
#731 closed-label contract (rulepack_contract_test.go) rightly refuses a pack
that references not-yet-emittable labels. So these promtool goldens live OUTSIDE
tests/rulepacks/ (which the #731 fixture contract scans) and are driven HERE:
compile the example fixture → a temp pack → run `promtool test rules` against it.

Coverage: fire / no-version main path / version fallback / maintenance suppress
/ selector filtering / ratio division-by-zero / absence scope-isolation /
p99 quantile / forecast / eq-match / mode routing / slo burn-rate tiers (one
golden per fixture file under fixtures/custom_alerts_promtool/ — all 7 recipes
plus the equals/mode_routing behavioral goldens).

Skips cleanly when promtool is absent (e.g. a CI job without it); runs fully in
the dev container and any promtool-equipped environment.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)
import compile_custom_alerts as cc  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"
_GOLDENS = sorted((_REPO / "tests" / "dx" / "fixtures" / "custom_alerts_promtool").glob("*.yaml"))

_PROMTOOL = shutil.which("promtool")
pytestmark = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")


@pytest.fixture(scope="module")
def compiled_pack(tmp_path_factory):
    """Compile the example fixture into a temp pack the goldens point at."""
    workdir = tmp_path_factory.mktemp("custom_alerts_promtool")
    pack = cc.build_pack(_EXAMPLES)
    (workdir / "rule-pack-custom-alerts.yaml").write_text(
        cc._render(pack["groups"]), encoding="utf-8"
    )
    return workdir


@pytest.mark.parametrize("golden", _GOLDENS, ids=lambda p: p.stem)
def test_promtool_golden(golden, compiled_pack):
    # co-locate the golden next to the compiled pack (its rule_files is same-dir)
    dest = compiled_pack / golden.name
    dest.write_text(golden.read_text(encoding="utf-8"), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", golden.name],
        cwd=compiled_pack, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, (
        f"promtool failed for {golden.name}:\n{result.stdout}\n{result.stderr}"
    )


def test_goldens_present():
    """Guard against an empty glob silently passing (echo-chamber)."""
    assert len(_GOLDENS) == 9, f"expected 9 goldens (7 recipes + equals + mode_routing), found {[p.name for p in _GOLDENS]}"


def test_adversarial_selector_value_compiles_to_valid_promql(tmp_path, compiled_pack):
    """ADR §S5: a spec-valid selector VALUE containing a double-quote is escaped
    by the compiler (_escape_value) into a well-formed quoted literal — promtool
    accepts it. Proves the Go preflight accepting any key-valid value is safe:
    the value can never break out of the quoted matcher / inject (CI backstop)."""
    confd = tmp_path / "conf.d"
    confd.mkdir()
    (confd / "shop.yaml").write_text(
        'tenants:\n  shop-a:\n    _custom_alerts:\n'
        '      - {recipe: rate, name: adv, metric: http_requests_total, '
        'selectors: {path: \'a"b\'}, op: ">", window: 5m, threshold: "1:warning"}\n',
        encoding="utf-8")
    pack = cc.build_pack(confd)
    rendered = cc._render(pack["groups"])
    assert 'path="a\\"b"' in rendered          # the " is escaped, stays inside the literal
    out = tmp_path / "rule-pack-custom-alerts.yaml"
    out.write_text(rendered, encoding="utf-8")
    r = subprocess.run([_PROMTOOL, "check", "rules", str(out)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, f"promtool rejected escaped selector:\n{r.stdout}\n{r.stderr}"


def test_one_tenants_two_alerts_of_one_shape_leave_the_shared_rule_working(tmp_path, da_guard_env):
    """#3112: the pack has ONE rule per (recipe_id, severity) for every tenant,
    joined on(tenant, version) to the tenant's threshold row. A tenant writing
    two alerts of that shape and severity under different names used to get
    both rows served — two series on the joined side, so the rule's
    evaluation failed ("many-to-many matching not allowed") and NO tenant of
    the shape alerted. Driven end to end: the rows are what da-guard
    served-values says the exporter serves, the rules are the compiled pack."""
    import yaml
    sys.path.insert(0, str(_REPO / "scripts" / "tools"))
    import _lib_tenant_values as tv

    confd = tmp_path / "conf.d"
    confd.mkdir()
    thr = "recipe: threshold, metric: m1, op: '>', window: 5m"
    (confd / "_defaults.yaml").write_text("defaults:\n  cpu: 80\n", encoding="utf-8")
    (confd / "ta.yaml").write_text(
        f"tenants:\n  ta:\n    _custom_alerts:\n      - {{{thr}, name: a, threshold: '5:warning'}}\n"
        f"      - {{{thr}, name: b, threshold: '7:warning'}}\n", encoding="utf-8")
    (confd / "tb.yaml").write_text(
        f"tenants:\n  tb:\n    _custom_alerts:\n      - {{{thr}, name: c, threshold: '5:warning'}}\n",
        encoding="utf-8")
    pack = cc.build_pack(confd)
    (tmp_path / "pack.yaml").write_text(cc._render(pack["groups"]), encoding="utf-8")

    def label_set(labels: dict) -> str:
        return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}"

    series = []
    for tenant, values in tv.load_served_tree(confd).tenants.items():
        for row in values.values.get("_custom_alerts") or []:
            labels = {**row["labels"], "component": "custom", "metric": row["metric"],
                      "severity": row["severity"], "tenant": tenant}
            series.append((f"user_threshold{label_set(labels)}", f"{row['value']}x20"))
        series.append((f'm1{{tenant="{tenant}"}}', "10x20"))
    (tmp_path / "test.yaml").write_text(yaml.safe_dump({
        "rule_files": ["pack.yaml"],
        "evaluation_interval": "1m",
        "tests": [{
            "interval": "1m",
            "input_series": [{"series": s, "values": v} for s, v in series],
            "promql_expr_test": [{
                "expr": 'count by (tenant) (ALERTS{alertstate="firing", component="custom"})',
                "eval_time": "10m",
                "exp_samples": [{"labels": '{tenant="ta"}', "value": 1},
                                {"labels": '{tenant="tb"}', "value": 1}],
            }],
        }],
    }, sort_keys=False), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", "test.yaml"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
