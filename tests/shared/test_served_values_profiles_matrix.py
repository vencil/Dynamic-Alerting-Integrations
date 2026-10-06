"""profiles 列語意矩陣：`_profile` 寫在哪裡，答案都要與 Go 的 da-guard effective 一致（#2115 0-B／B3）。

讀取端：`validate_config.check_profiles`（validate-config 的 profiles 列）。

`_profile` 是保留鍵，依 #2115 (c) 看「寫法＋繼承」：`da-guard effective` 的
`effective_config["_profile"]` 是寫下的名字（原文，含繼承），`profile` 是 exporter 實際
綁定的 profile（沒有綁就是 null）。有名字卻沒綁定的租戶要被點名。

四個位置沿用 `test_served_values_readers_matrix` 的形狀：同一個值（`_profile: nope`，
一個沒有定義的 profile）只寫在根 `defaults:`、平台檔 `tenants:`、租戶檔、子樹
`_defaults.yaml` 其中之一；tenant-b 是對照組（`_profile: nope` 永遠寫在自己的租戶檔）。

舊讀取端（33d56451 以前）只讀各租戶檔本身的 `_profile`、只認 `_profiles.yaml` 裡的
profile、把平面格式檔當租戶、對 exporter 丟掉的檔照樣回報：所以只有「租戶檔」那格答對。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv
import validate_config as vc
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
    assert row["details"] == ["1 tenants scanned, 1 profile refs, 0 profiles defined"], row


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
    assert row["details"] == ["2 tenants scanned, 1 profile refs, 1 profiles defined"], row
    (conf_d / "team" / "deep" / "tenant-a.yaml").write_text(
        "tenants:\n  tenant-a:\n    _profile: nope\n", encoding="utf-8")
    assert _named(vc.check_profiles(str(conf_d))) == {"tenant-a"}


# ── 平面格式檔（R3）：不是租戶，照 served 的 skipped 具名 WARN ───────────────

def test_flat_file_is_not_a_tenant_and_is_named(tmp_path, capsys):
    conf_d = _tree(tmp_path, {
        "_defaults.yaml": _BASE,
        "_profiles.yaml": _PROFILES,
        "flat-a.yaml": "_profile: nope\nmysql_connections: 5\n",
        "tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
    })
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.PASS, row
    assert row["details"] == ["1 tenants scanned, 0 profile refs, 1 profiles defined"], row
    assert "WARN: flat-a.yaml: declares no tenant" in capsys.readouterr().err


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


def test_missing_da_guard_is_a_caller_error(tmp_path, monkeypatch):
    conf_d = _tree(tmp_path, POSITIONS["tenant-file"])
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    row = vc.check_profiles(str(conf_d))
    assert row["status"] == vc.FAIL and row["caller_error"] is True, row
    assert row["hint"] == vc._PROFILES_NO_DA_GUARD_HINT, row
