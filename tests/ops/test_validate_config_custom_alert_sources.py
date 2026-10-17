"""validate-config `custom_alert_sources`（#3042）：exporter 送出（或停用、重新啟用
後會送出）的自訂告警，每一條都編得出規則；只報不擋。

編譯器照 exporter 自己的讀法編（da-guard served-values 的 `custom_alerts`），所以
沒有規則只剩一種原因：兩邊對同一條的讀法不同（recipe_id 不同，或編譯器拒收
exporter 收下的設定）。判斷交給編譯器的 `entries.shapes_from_entries`，這一列只
決定怎麼報。exporter 拒收或丟掉的條目不歸這一列報（da-guard 與 tenant-api 報）。
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
    return vc.check_custom_alert_sources(d, entries_mod=vc._custom_alert_compiler())


@pytest.fixture
def parted(monkeypatch):
    """The compiler's recipe_id for an entry named `bad` no longer matches the
    exporter's — the two readings have parted for that one entry."""
    entries = vc._custom_alert_compiler()
    real = entries._shape.recipe_id

    def recipe_id(inst):
        rid = real(inst)
        return rid + "_parted" if inst.get("name") == "bad" else rid
    monkeypatch.setattr(entries._shape, "recipe_id", recipe_id)
    return entries


@pytest.mark.parametrize("files", [
    {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("own", "m_own")},
    {"_platform.yaml": "tenants:\n  t1:\n    _custom_alerts:\n" + _rec("plat", "m_plat")},
    {"_profiles.yaml": "profiles:\n  p1:\n    _custom_alerts:\n" + _rec("prof", "m_prof"),
     "team/t1.yaml": _T1 + "    _profile: p1\n"},
], ids=["tenant", "root-platform", "profile"])
def test_every_served_alert_has_a_rule(tmp_path, files):
    """Wherever the exporter takes a tenant's list from, the compiler builds
    the rule from the same reading."""
    r = _row(_tree(tmp_path, files))
    assert r["status"] == vc.PASS, r
    assert r["details"] == ["1 custom alert(s) the exporter serves or has switched off, each with "
                            "a rule the compiler builds"]


def test_an_alert_the_compiler_reads_differently_never_alerts(tmp_path, parted):
    d = _tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("ok", "m1") + _rec("bad", "m2")})
    r = _row(d)
    assert r["status"] == vc.WARN and not r["caller_error"]
    assert r["hint"] == vc._CUSTOM_ALERT_SOURCES_HINT
    assert len(r["details"]) == 1, r["details"]
    line = r["details"][0]
    assert line.startswith("team/t1.yaml: tenant t1 `_custom_alerts[1]` (`bad`, threshold__m2__gt__w5m__for1m) "
                           "is served, but no rule can be compiled for it — it never alerts: "), line
    assert "threshold__m2__gt__w5m__for1m_parted" in line


def test_a_switched_off_alert_is_judged_too(tmp_path, parted):
    """A disabled entry gets a rule (switching it back on needs no
    recompile), so one the compiler reads differently is reported as well."""
    d = _tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("bad", "m2", threshold="disable")})
    r = _row(d)
    assert r["status"] == vc.WARN
    assert "is switched off (served once back on), but no rule can be compiled" in r["details"][0]


@pytest.mark.parametrize("recipes", [
    _rec("a", "m1") + _rec("a2", "m1", threshold="20:warning"),   # duplicate of [0]
    _rec("a", "m1") + "      - {recipe: bogus, name: x, metric: m, threshold: '1'}\n",  # rejected
], ids=["duplicate", "rejected"])
def test_entries_the_exporter_does_not_serve_are_not_this_rows(tmp_path, recipes):
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + recipes}))
    assert r["status"] == vc.PASS, r
    assert r["details"][0].startswith("1 custom alert(s) ")


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


def test_a_da_guard_without_the_entries_is_not_compared(tmp_path, monkeypatch):
    real = vc.load_served_tree

    def stale(*a, **k):
        tree = real(*a, **k)
        return tree._replace(tenants={t: v._replace(custom_alerts=None) for t, v in tree.tenants.items()})
    monkeypatch.setattr(vc, "load_served_tree", stale)
    r = _row(_tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("own", "m_own")}))
    assert r["status"] == vc.WARN
    assert "has no `custom_alerts`" in r["details"][0] and "older than this tool" in r["details"][0]


def test_the_row_is_reported_and_does_not_fail_the_run(tmp_path):
    d = _tree(tmp_path, {"team/t1.yaml": _T1 + "    _custom_alerts:\n" + _rec("own", "m_own")})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["custom_alert_sources"]["status"] == vc.PASS
    assert p.returncode == 0, p.stderr


def test_without_the_compiler_the_row_is_absent(tmp_path, monkeypatch, capsys):
    """The da-tools image ships no compiler: the row is not run, so the
    report does not claim a comparison it never made."""
    monkeypatch.setattr(vc, "_custom_alert_compiler", lambda: None)
    monkeypatch.setattr(sys, "argv", ["validate_config.py", "--config-dir", _tree(tmp_path, {}), "--json"])
    with pytest.raises(SystemExit):
        vc.main()
    assert "custom_alert_sources" not in {r["check"] for r in json.loads(capsys.readouterr().out)}
