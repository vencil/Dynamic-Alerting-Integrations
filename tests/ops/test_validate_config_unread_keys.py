"""validate-config `unread_keys`（ADR-038 第二步）：沒有任何讀取端讀的鍵，只報不擋。

判決來自共用判決 `_lib_key_verdict`；這一列只負責「怎麼報」：一律 WARN、不影響
結束碼，da-guard 不可用或失敗時說判決未完成，不拒收（決定 5）。
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

_DEFAULTS = "defaults:\n  mysql_connections: 80\n  mysql_slave_lag: 30\n  redis_memory: 70\n"


def _tree(root: Path, files: dict[str, str]) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return str(root)


def test_each_finding_is_one_warn_line(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS,
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: 70\n    mysql_slave_lagg: disable\n"
                     "    redis_memory: disable\n    redis_memory_critical: 90\n"}))
    assert r["status"] == vc.WARN
    assert r["details"] == [
        "db-a.yaml: error: unread `mysql_conections` (no_row) — tenant(s) db-a",
        "db-a.yaml: warning: disable_noop `mysql_slave_lagg` (disable_noop) — tenant(s) db-a",
        "db-a.yaml: warning: disable_partial `redis_memory`; still served: redis_memory_critical"
        " — tenant(s) db-a",
    ]


def test_a_platform_side_finding_is_one_line_with_its_tenants(tmp_path):
    files = {"_defaults.yaml": _DEFAULTS, "_profiles.yaml": "profiles:\n  p1:\n    mysql_conections: 70\n"}
    for t in ("ta", "tb"):
        files[f"{t}.yaml"] = f"tenants:\n  {t}:\n    _profile: p1\n"
    r = vc.check_unread_keys(_tree(tmp_path, files))
    assert r["details"] == ["_profiles.yaml: notice: unread `mysql_conections` (no_row) — tenant(s) ta, tb"]


def test_a_clean_tree_passes(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {
        "_defaults.yaml": _DEFAULTS, "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: 70\n"}))
    assert r["status"] == vc.PASS


def test_undecidable_is_said(tmp_path):
    r = vc.check_unread_keys(_tree(tmp_path, {"ta.yaml": "tenants:\n  ta:\n    mysql_conections: 70\n"}))
    assert r["status"] == vc.WARN
    assert r["details"][-1] == "no root platform file was read: 1 threshold key(s) cannot be judged (undecidable)"


@pytest.mark.parametrize("exc", [
    tv.DaGuardNotFoundError("no da-guard"),
    tv.DaGuardError("boom", 2, "boom"),
    RuntimeError("anything else"),
])
def test_a_verdict_that_cannot_be_given_is_a_warn_not_a_refusal(tmp_path, monkeypatch, exc):
    """da-guard 不可用或失敗：只說判決未完成，不 FAIL、不 caller error（決定 5）。"""
    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(vc.key_verdict, "judge_tree", boom)
    monkeypatch.setattr(vc, "_effective_tree", lambda _d: None)
    r = vc.check_unread_keys(_tree(tmp_path, {"_defaults.yaml": _DEFAULTS}))
    assert r["status"] == vc.WARN and not r["caller_error"]
    assert r["details"][0].startswith("verdict incomplete: ")


def test_the_row_does_not_change_the_exit_code(tmp_path):
    """只報不擋：拼錯的閾值鍵今天只是其他列的 WARN，加上這一列仍是 0。"""
    d = _tree(tmp_path, {"_defaults.yaml": _DEFAULTS,
                         "db-a.yaml": "tenants:\n  db-a:\n    mysql_conections: \"70\"\n"})
    p = subprocess.run([sys.executable, vc.__file__, "--config-dir", d, "--json"],
                       capture_output=True, encoding="utf-8", timeout=600)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["unread_keys"]["status"] == vc.WARN
    assert p.returncode == 0, p.stderr
