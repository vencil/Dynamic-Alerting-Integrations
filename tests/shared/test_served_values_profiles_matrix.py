"""profiles 列語意矩陣：`_profile` 寫在哪裡，答案都要與 Go 的 da-guard effective 一致（#2115 0-B／B3）。

讀取端：`validate_config.check_profiles`（validate-config 的 profiles 列）。

`_profile` 是保留鍵，依 #2115 (c) 看「寫法＋繼承」：`da-guard effective` 的
`effective_config["_profile"]` 是寫下的名字（原文，含繼承），`profile` 是 exporter 實際
綁定的 profile（沒有綁就是 null）。有名字卻沒綁定的租戶要被點名。

四個位置沿用 `test_served_values_readers_matrix` 的形狀：同一個值（`_profile: nope`，
一個沒有定義的 profile）只寫在根 `defaults:`、平台檔 `tenants:`、租戶檔、子樹
`_defaults.yaml` 其中之一；tenant-b 是對照組（`_profile: nope` 永遠寫在自己的租戶檔）。

profiles 列只跑 `da-guard effective`（不跑 served-values）：沒有租戶的檔取自 effective 的
`skipped`，進列的明細。

舊讀取端（33d56451 以前）只讀各租戶檔本身的 `_profile`、只認 `_profiles.yaml` 裡的
profile、把平面格式檔當租戶、對 exporter 丟掉的檔照樣回報：所以只有「租戶檔」那格答對。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import validate_config as vc
from _platform_fs import require_shebang_scripts
from test_effective_values_parity import _doc, _fake_da_guard
from test_served_values_readers_matrix import _tree

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

_PROFILES = "profiles:\n  gold:\n    mysql_connections: 70\n"
_BASE = "defaults:\n  mysql_connections: 80\n"
_A_PLAIN = "tenants:\n  tenant-a:\n    mysql_connections: \"60\"\n"
_B = "tenants:\n  tenant-b:\n    _profile: nope\n"
_UNKNOWN_B = 'tenant=tenant-b: _profile references unknown profile "nope"'

POSITIONS = {
    "defaults": {
        "_defaults.yaml": _BASE + "  _profile: nope\n",
        "_profiles.yaml": _PROFILES,
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "platform-tenants": {
        "_defaults.yaml": _BASE + "tenants:\n  tenant-a:\n    _profile: nope\n",
        "_profiles.yaml": _PROFILES,
        "tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
    "tenant-file": {
        "_defaults.yaml": _BASE,
        "_profiles.yaml": _PROFILES,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _profile: nope\n",
        "tenant-b.yaml": _B,
    },
    "subtree": {
        "_defaults.yaml": _BASE,
        "_profiles.yaml": _PROFILES,
        "team/_defaults.yaml": "defaults:\n  _profile: nope\n",
        "team/tenant-a.yaml": _A_PLAIN,
        "tenant-b.yaml": _B,
    },
}


def _want_from_effective(conf_d: Path) -> set[str]:
    """The tenants Go reports a `_profile` name for but binds no profile."""
    return {t for t, e in tv.load_effective(conf_d).items()
            if isinstance(e.effective_config.get("_profile"), str) and e.profile is None}


def _named(row: dict) -> set[str]:
    return {d.split(":", 1)[0].removeprefix("tenant=") for d in row["details"]
            if d.startswith("tenant=")}


@pytest.fixture(autouse=True)
def _da_guard(da_guard_env):
    return da_guard_env


# ── 四位置：結論與 da-guard effective 一致 ─────────────────────────────────

@pytest.mark.parametrize("where", ["platform-tenants", "tenant-file", "subtree"])
def test_profiles_row_matches_effective(where, tmp_path):
    conf_d = _tree(tmp_path, POSITIONS[where])
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.WARN, row
    assert _named(row) == _want_from_effective(conf_d) == {"tenant-a", "tenant-b"}, (where, row)
    assert _UNKNOWN_B in row["details"], row   # 對照組三格不變
    a = [d for d in row["details"] if d.startswith("tenant=tenant-a:")]
    if where == "subtree":
        # exporter 不從 defaults 檔綁 profile：點名寫的檔，不說成「沒有這個 profile」
        assert a == ['tenant=tenant-a: _profile "nope" comes from team/_defaults.yaml, and the '
                     "exporter binds no profile from a defaults file (only from the tenant's own "
                     "entry or a root platform file's `tenants:` entry)"], row
    else:
        assert a == ['tenant=tenant-a: _profile references unknown profile "nope"'], row


def test_profile_in_root_defaults_drops_the_file_and_fails_closed(tmp_path):
    """根 `defaults:` 裡的 `_profile`：exporter 整份 `_defaults.yaml` 丟掉（所有租戶失去根
    預設值）。列 FAIL、指名該檔與 exporter 的原因，不評估、不回報 WARN 了事。"""
    conf_d = _tree(tmp_path, POSITIONS["defaults"])
    with pytest.raises(tv.ParseFailedError):   # 前提：Go 丟掉這個檔
        tv.load_effective(conf_d)
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is False, row
    assert row["hint"] == vc._PROFILES_TREE_UNREADABLE_HINT, row
    assert any("_defaults.yaml" in d and "do not decode" in d for d in row["details"]), row


# ── profile 的定義：exporter 讀所有根平台檔的 `profiles:`，不只 `_profiles.yaml` ──

def test_profile_defined_outside_profiles_yaml_is_bound(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE + _PROFILES,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _profile: gold\n",
    })
    assert tv.load_effective(conf_d)["tenant-a"].profile == "gold"   # 前提
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.PASS, row
    assert row["details"] == ["1 tenants scanned, 1 profile refs, 0 profiles defined in _profiles.yaml"], row


# ── 遞迴保留：子目錄的租戶照樣被檢查與計數 ───────────────────────────────

def test_nested_tenant_is_checked_and_counted(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "_profiles.yaml": _PROFILES,
        "team/deep/tenant-a.yaml": "tenants:\n  tenant-a:\n    _profile: gold\n",
        "tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
    })
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.PASS, row
    assert row["details"] == ["2 tenants scanned, 1 profile refs, 1 profiles defined in _profiles.yaml"], row
    (conf_d / "team" / "deep" / "tenant-a.yaml").write_text(
        "tenants:\n  tenant-a:\n    _profile: nope\n", encoding="utf-8")
    assert _named(vc.check_profiles(str(conf_d))) == {"tenant-a"}


# ── 平面格式檔（R3）：不是租戶，照 effective 的 skipped 在列的明細具名（F4）──────

_FLAT_TREE = {
    "_defaults.yaml": _BASE,
    "_profiles.yaml": _PROFILES,
    "flat-a.yaml": "_profile: nope\nmysql_connections: 5\n",
    "tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
}
_FLAT_LINE = ("not a tenant: flat-a.yaml: declares no tenant: a file whose name does not start "
              "with `_` is read only through its `tenants:` mapping, and this one has none (or an "
              "empty one)")


def test_flat_file_is_not_a_tenant_and_is_named_in_the_row(tmp_path, capsys):
    """skipped 進列的 details（因而進 --json），不再只印 stderr；狀態不因它改變。"""
    conf_d = _tree(tmp_path, _FLAT_TREE)
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.PASS, row
    assert row["details"] == ["1 tenants scanned, 0 profile refs, 1 profiles defined in _profiles.yaml",
                              _FLAT_LINE], row
    assert "flat-a.yaml" not in capsys.readouterr().err   # 這一列自己不再印 stderr


def test_flat_file_reaches_json_and_is_warned_once_with_policy_dsl(tmp_path):
    """--json 帶到 skipped；加 --policy-dsl（policy 列也載入租戶）時 stderr 只 WARN 一次。
    修前：profiles 列與 policy 列各印一次 `WARN: flat-a.yaml: …`，--json 裡沒有。"""
    conf_d = _tree(tmp_path, _FLAT_TREE)
    dsl = tmp_path / "policy.yaml"
    dsl.write_text("policies:\n  - name: mc\n    target: mysql_connections\n"
                   "    operator: required\n    severity: warning\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir", str(conf_d),
                        "--policy-dsl", str(dsl), "--json"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["policy_dsl"]["status"] != vc.FAIL, rows["policy_dsl"]   # 前提：policy 列真的載入了租戶
    assert _FLAT_LINE in rows["profiles"]["details"], rows["profiles"]
    assert p.stderr.count("WARN: flat-a.yaml:") == 1, p.stderr


# ── F2：profiles 列只跑 effective；served-values 拒收的樹（/metrics 的事）不關它 ──

def test_root_and_tenant_critical_tree_passes_like_main(tmp_path):
    """根層與租戶都寫 `X_critical`：served-values rc 2（同一 key 兩列不同值），effective
    rc 0。profiles 列只需要 effective，main（33d56451）上這棵樹 PASS，現在也要 PASS。"""
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": ("defaults:\n  mysql_connections: 80\n"
                           "  mysql_connections_critical: 95\n"),
        "_profiles.yaml": _PROFILES,
        "t1.yaml": ("tenants:\n  t1:\n    _profile: gold\n    mysql_connections: 70\n"
                    "    mysql_connections_critical: 90\n"),
    })
    with pytest.raises(tv.ServedValuesError):   # 前提：served-values 拒收這棵樹
        tv.load_served_tree(conf_d)
    assert tv.load_effective(conf_d)["t1"].profile == "gold"   # 前提：effective 照答
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.PASS, row
    assert row["details"] == ["1 tenants scanned, 1 profile refs, 1 profiles defined in _profiles.yaml"], row


# ── fail-closed：exporter 丟掉／讀不到的檔、沒有 da-guard ──────────────────

def test_file_the_exporter_drops_fails_closed(tmp_path):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "_profiles.yaml": _PROFILES,
        "tenant-b.yaml": _B,
        "_platform.yaml": "tenants:\n  tenant-b: 3\n",
    })
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is False, row
    assert any(d.startswith("ERROR: cannot read") and "_platform.yaml" in d for d in row["details"]), row
    assert any("cannot unmarshal" in d for d in row["details"]), row   # exporter 自己的原因
    p =subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir", str(conf_d),
                        "--json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert rows["profiles"]["status"] == vc.FAIL and p.returncode == 1, (p.returncode, rows["profiles"])
    assert "Traceback" not in p.stderr, p.stderr


def test_empty_config_dir_fails_with_the_no_config_hint(tmp_path):
    """空 conf.d（F3）：exporter 對它拒絕啟動（"no .yaml files found"），所以這一列 FAIL、
    給 NO_CONFIG 的建議，不是「0 tenants」的 PASS。main 是 PASS／rc 0：行為變更，已揭露於
    changelog.d。#2725：--config-dir 底下沒有任何設定檔是呼叫端的錯（caller error、rc 2），
    不是樹裡某個檔的問題。"""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is True, row
    assert row["hint"] == vc._PROFILES_NO_CONFIG_FILE_HINT, row
    assert any("no .yaml files found" in d for d in row["details"]), row
    p = subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir", str(conf_d),
                        "--json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert p.returncode == 2, (p.returncode, rows["profiles"])
    assert rows["profiles"]["suggested_action"] == vc._PROFILES_NO_CONFIG_FILE_HINT, rows["profiles"]


def test_missing_da_guard_is_a_caller_error(tmp_path, monkeypatch):
    conf_d = _tree(tmp_path, POSITIONS["tenant-file"])
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is True, row
    assert row["hint"] == vc._PROFILES_NO_DA_GUARD_HINT, row
    assert "make da-guard-build" in row["hint"], row   # 指向 repo 內建出它的 target


def test_da_guard_older_than_this_tool_is_a_caller_error(tmp_path, monkeypatch):
    """effective 的輸出沒有 `skipped`（本工具之前建的 da-guard）：是 binary 太舊，不是設定檔
    壞了——caller error（rc 2），建議重建／升級 da-guard（指向 make da-guard-build），
    不叫人去修或刪設定檔。"""
    conf_d = _tree(tmp_path / "t", POSITIONS["tenant-file"])
    fake = _fake_da_guard(tmp_path, json.dumps(_doc()))   # 沒有 skipped 欄位
    monkeypatch.setenv("DA_GUARD_BINARY", fake)
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is True, row
    assert row["hint"] == vc._DA_GUARD_TOO_OLD_HINT, row
    assert "make da-guard-build" in row["hint"], row
    assert any("older than this tool" in d for d in row["details"]), row
    p = subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir", str(conf_d),
                        "--json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    assert p.returncode == 2, (p.returncode, rows["profiles"])
    assert rows["profiles"]["suggested_action"] == vc._DA_GUARD_TOO_OLD_HINT, rows["profiles"]


# ── 被丟掉的檔：原因取自 effective 自己的 stderr，不再補跑 served-values（#2115 F4）──

@pytest.mark.parametrize("files,reason", [
    pytest.param({"_defaults.yaml": 'defaults:\n  mysql_threads_running: "abc"\n',
                  "tx.yaml": "tenants:\n  tx: {}\n"},
                 "cannot unmarshal !!str `abc` into float64", id="root-defaults-type"),
    pytest.param({"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
                  "tx.yaml": "tenants:\n  tx: [a]\n", "ty.yaml": "tenants:\n  ty: {}\n"},
                 "cannot unmarshal !!seq into map[string]config.ScheduledValue",
                 id="tenant-body-list"),
])
def test_dropped_file_reason_comes_from_effective_alone(tmp_path, monkeypatch, files, reason):
    """`_with_exporter_reasons` 曾在 effective 失敗後補跑 served-values 借原因文字；
    `da-guard effective` 的 stderr 自 #2115 C 起就帶原因，補跑已拿掉。量測：profiles 與
    values_not_served 兩列 FAIL、明細有 exporter 的原因行，而 da-guard 只被呼叫
    `effective`（以計數 wrapper 記錄子命令）。"""
    require_shebang_scripts()  # the counting wrapper is a `#!` script
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    for name, body in files.items():
        (conf_d / name).write_text(body, encoding="utf-8")
    log = tmp_path / "calls.log"
    wrapper = tmp_path / "dg-wrap.sh"
    wrapper.write_text(f'#!/bin/sh\necho "$1" >> "{log}"\nexec "$DA_GUARD_REAL" "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("DA_GUARD_REAL", os.environ["DA_GUARD_BINARY"])
    monkeypatch.setenv("DA_GUARD_BINARY", str(wrapper))
    p = subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir",
                        str(conf_d), "--json"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    rows = {r["check"]: r for r in json.loads(p.stdout)}
    for check in ("profiles", "values_not_served"):
        row = rows[check]
        assert row["status"] == vc.FAIL and row["caller_error"] is False, row
        assert any(d.startswith("  da-guard| ") and reason in d for d in row["details"]), row
    assert log.read_text(encoding="utf-8").split() == ["effective"]
