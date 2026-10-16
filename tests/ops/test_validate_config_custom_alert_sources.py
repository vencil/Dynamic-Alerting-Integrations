"""validate-config `custom_alert_sources`（#3042）：自訂告警編譯器與 exporter
對不上的地方，只報不擋。

兩邊都由各自的程式回答（編譯器的 `build_shapes`、da-guard served-values），
這一列只按（租戶、名稱、recipe_id）比對、決定怎麼報。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import validate_config as vc

pytestmark = pytest.mark.usefixtures("da_guard_env")

_ROOT = "defaults:\n  mysql_connections: 80\n"
_T1 = "tenants:\n  t1:\n    mysql_connections: \"70\"\n"


def _rec(name: str, metric: str, indent: int = 6, threshold: str = "10:warning") -> str:
    return (" " * indent + f"- {{recipe: threshold, name: {name}, metric: {metric}, op: '>', "
            f"window: 5m, threshold: '{threshold}'}}\n")


def _tree(root: Path, files: dict[str, str]) -> str:
    base = {"_defaults.yaml": _ROOT, "team/t1.yaml": _T1}
    base.update(files)
    for rel, text in base.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def _row(d: str) -> dict:
    return vc.check_custom_alert_sources(d, loader=vc._custom_alert_compiler())


def test_a_tenant_own_recipe_passes(tmp_path):
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("own", "m_own")}))
    assert r["status"] == vc.PASS, r


@pytest.mark.parametrize("files, source", [
    ({"_defaults.yaml": _ROOT + "_custom_alerts:\n" + _rec("n", "m", indent=2)},
     "an inherited `_defaults.yaml`"),
    ({"team/_defaults.yaml": "_custom_alerts:\n" + _rec("n", "m", indent=2)},
     "an inherited `_defaults.yaml`"),
    ({"team/_other.yaml": "tenants:\n  t1:\n    _custom_alerts:\n" + _rec("n", "m")},
     "team/_other.yaml"),
], ids=["root-defaults-top", "subtree-defaults-top", "subtree-other-file"])
def test_a_rule_the_exporter_serves_nothing_for_never_fires(tmp_path, files, source):
    r = _row(_tree(tmp_path, files))
    assert r["status"] == vc.WARN
    assert r["details"] == [f"{source}: `n` (threshold__m__gt__w5m__for1m) is compiled, but the "
                            "exporter serves no threshold for it — the rule never fires — tenant(s) t1"]
    assert r["hint"] == vc._CUSTOM_ALERT_SOURCES_HINT


@pytest.mark.parametrize("files, source", [
    ({"_defaults.yaml": _ROOT + "tenants:\n  t1:\n    _custom_alerts:\n" + _rec("n", "m")},
     "_defaults.yaml"),
    ({"_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + _rec("n", "m"),
      "team/t1.yaml": _T1 + "    _profile: p1\n"},
     "_profiles.yaml"),
], ids=["root-defaults-tenants", "profile"])
def test_a_served_threshold_no_rule_reads_never_alerts(tmp_path, files, source):
    r = _row(_tree(tmp_path, files))
    assert r["details"] == [f"{source}: `n` (threshold__m__gt__w5m__for1m) is served, but no rule "
                            "is compiled for it — it never alerts — tenant(s) t1"]


def test_a_same_name_clash_is_wrong_on_both_sides(tmp_path):
    """The exporter serves the tenant's own recipe; the compiler compiles the
    inherited one and quarantines the tenant's."""
    r = _row(_tree(tmp_path, {
        "_defaults.yaml": _ROOT + "_custom_alerts:\n" + _rec("same", "m_root", indent=2),
        "team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("same", "m_own")}))
    assert r["details"] == [
        "an inherited `_defaults.yaml`: `same` (threshold__m_root__gt__w5m__for1m) is compiled, "
        "but the exporter serves no threshold for it — the rule never fires — tenant(s) t1",
        "team/t1.yaml: `same` (threshold__m_own__gt__w5m__for1m) is served, but no rule is "
        "compiled for it — it never alerts — tenant(s) t1"]


def test_one_line_per_source_with_every_tenant_it_reaches(tmp_path):
    r = _row(_tree(tmp_path, {
        "_defaults.yaml": _ROOT + "_custom_alerts:\n" + _rec("n", "m", indent=2),
        "t0.yaml": "tenants:\n  t0:\n    mysql_connections: \"60\"\n"}))
    assert len(r["details"]) == 1 and r["details"][0].endswith("tenant(s) t0, t1")


def test_a_disabled_recipe_is_compiled_and_not_served_by_design(tmp_path):
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              + _rec("turned_off", "m_off", threshold="disable")
                              + "      - {recipe: slo_burn_rate, name: slo, metric: m_err, "
                                "denominator_metric: m_tot, objective: 'disable'}\n"}))
    assert r["status"] == vc.PASS, r


@pytest.mark.parametrize("exc", [
    tv.DaGuardNotFoundError("no da-guard"),
    tv.DaGuardError("boom", 2, "boom"),
    RuntimeError("anything else"),
])
def test_an_unreadable_exporter_side_is_a_warn_not_a_refusal(tmp_path, monkeypatch, exc):
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(vc, "load_served_tree", boom)
    r = _row(_tree(tmp_path, {}))
    assert r["status"] == vc.WARN and not r["caller_error"]
    assert r["details"][0].startswith("comparison incomplete: ")
    assert r["hint"] == vc._CUSTOM_ALERT_SOURCES_INCOMPLETE_HINT


def test_the_row_does_not_change_the_exit_code(tmp_path):
    d = _tree(tmp_path, {"_defaults.yaml": _ROOT + "_custom_alerts:\n" + _rec("n", "m", indent=2)})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["custom_alert_sources"]["status"] == vc.WARN
    assert p.returncode == 0, p.stderr


def test_without_the_compiler_the_row_is_absent(tmp_path, monkeypatch, capsys):
    """The da-tools image ships no compiler: the row is not run, so the
    report does not claim a comparison it never made."""
    monkeypatch.setattr(vc, "_custom_alert_compiler", lambda: None)
    monkeypatch.setattr(sys, "argv", ["validate_config.py", "--config-dir", _tree(tmp_path, {}), "--json"])
    with pytest.raises(SystemExit):
        vc.main()
    assert "custom_alert_sources" not in {r["check"] for r in json.loads(capsys.readouterr().out)}


@pytest.mark.parametrize("threshold", ["disable", "Disable", "disabled", "off:warning", "FALSE"])
def test_every_spelling_the_exporter_switches_off_is_left_out(tmp_path, threshold):
    """The exporter's own opt-out test (isDisabled): disable / disabled / off
    / false, any case — compiled, not served, by design."""
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              + _rec("turned_off", "m_off", threshold=threshold)}))
    assert r["status"] == vc.PASS, r


def test_the_name_does_not_decide_a_match(tmp_path):
    """A rule joins on recipe_id and severity; a name the two sides spell
    differently (unquoted `on`: Python True, Go "on") is the same alert."""
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              "      - {recipe: threshold, name: on, metric: m, op: '>', "
                              "window: 5m, threshold: '10:warning'}\n"}))
    assert r["status"] == vc.PASS, r


@pytest.mark.parametrize("files", [
    {"team/t1.yaml": _T1 + "    _custom_alerts: 5\n"},
    {"_defaults.yaml": _ROOT + "_custom_alerts: 5\n"},
], ids=["tenant", "root-defaults-top"])
def test_a_tree_the_compiler_cannot_walk_is_a_warn_not_a_refusal(tmp_path, files):
    """`_custom_alerts` that is not a list stops the compiler's walk: the row
    says nothing was compared — it does not FAIL, and the run's exit code is
    the one the other rows give (here 1, from json_schema)."""
    d = _tree(tmp_path, files)
    r = _row(d)
    assert r["status"] == vc.WARN and not r["caller_error"], r
    assert r["details"][0].startswith("comparison incomplete: the compiler's custom alerts")
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {x["check"]: x for x in json.loads(p.stdout)}
    assert rows["custom_alert_sources"]["status"] == vc.WARN
    assert p.returncode in (0, 1), p.returncode


def test_a_served_row_another_tenant_got_a_rule_for_does_alert(tmp_path):
    """Rules are per (recipe_id, severity) for the whole pack, joined on
    tenant: t2's served row (from the root `_defaults.yaml` `tenants:`, which
    the compiler does not read) is alerted on by the rule t1's own recipe
    compiled."""
    r = _row(_tree(tmp_path, {
        "_defaults.yaml": _ROOT + "tenants:\n  t2:\n    _custom_alerts:\n" + _rec("n", "m"),
        "team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("n", "m"),
        "t2.yaml": "tenants:\n  t2:\n    mysql_connections: \"5\"\n"}))
    assert r["status"] == vc.PASS, r


def test_a_switched_off_recipe_still_compiles_the_rule(tmp_path):
    """`a` is off (compiled, not served) and takes the shape; `b`, same shape
    and severity, is quarantined but the only one served — the rule `a`
    compiled alerts on it: nothing to report."""
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              + _rec("a", "m", threshold="disable:warning")
                              + _rec("b", "m", threshold="10:warning")}))
    assert r["status"] == vc.PASS, r


_SHARED = ("team/t1.yaml: {names} (threshold__m__gt__w5m__for1m, warning) are served for one "
           "tenant with the same shape and severity — the compiler takes at most one recipe per "
           "tenant, shape and severity — tenant(s) t1")


@pytest.mark.parametrize("recipes, names", [
    ([("a", "10:warning"), ("b", "20:warning")], "`a`, `b`"),
    # The one the compiler keeps is switched off (serves no row): both served
    # ones are quarantined — the line names them without picking a "kept" one.
    ([("a", "disable:warning"), ("b", "10:warning"), ("c", "20:warning")], "`b`, `c`"),
], ids=["two", "kept-one-switched-off"])
def test_served_recipes_of_one_shape_and_severity_are_said(tmp_path, recipes, names):
    """The exporter serves each; the compiler takes one per tenant, shape and
    severity."""
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              + "".join(_rec(n, "m", threshold=th) for n, th in recipes)}))
    assert r["status"] == vc.WARN
    assert r["details"] == [_SHARED.format(names=names)]


def test_the_served_names_are_the_exporters_spelling(tmp_path):
    """Unquoted `on` is "on" to the exporter (True to PyYAML): the line names
    what is served, so no recipe is named that the exporter does not serve."""
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              "      - {recipe: threshold, name: on, metric: m, op: '>', "
                              "window: 5m, threshold: '10:warning'}\n"
                              + _rec("b", "m", threshold="20:warning")}))
    assert r["details"] == [_SHARED.format(names="`b`, `on`")]


def test_the_pass_line_counts_per_tenant_and_severity(tmp_path):
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n"
                              + _rec("a", "m", threshold="10:warning")
                              + _rec("b", "m", threshold="20:critical")}))
    assert r["details"] == ["2 compiled custom alert(s), counted per tenant and severity, "
                            "each with a served threshold; every served one has a rule"]


def test_a_root_platform_recipe_beside_the_tenants_own_never_fires(tmp_path):
    """The exporter takes one source per tenant, its own entry first; the
    compiler takes both."""
    r = _row(_tree(tmp_path, {
        "_platform.yaml": "tenants:\n  t1:\n    _custom_alerts:\n" + _rec("a", "m"),
        "team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("b", "m2")}))
    assert r["details"] == ["_platform.yaml: `a` (threshold__m__gt__w5m__for1m) is compiled, but "
                            "the exporter serves no threshold for it — the rule never fires — "
                            "tenant(s) t1"]
