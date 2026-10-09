"""validate-config 經 da-guard 讀樹失敗時，錯要歸給對的對象（#2725，含 #2115 D）。

- da-guard 本身不可用（不可執行、回 served-values／effective 不會用的結束碼、輸出不是它的
  JSON）→ `broken`；不認得本工具跑的子命令與旗標（v2.9.x 沒有 served-values／effective、
  更早的 served-values 沒有 `--schedules`）→ `stale`。兩者都是 caller error（rc 2），提示
  指向 binary，不叫人修設定檔。判定只看結束碼與「同一組 argv 加 `-h`」的結束碼：不寫暫存
  樹、不從 stderr 挑字。
- 對照組：可用的 da-guard 拒收的樹（F0）仍是樹的問題（caller_error False）。
- policy_dsl 列：`--policy-dsl` 載入失敗時不說規則來自它；根 `_defaults.yaml` 是斷掉的
  symlink 時不說「沒有根載體」；沒有閾值被評估到／有 unserved 值時說出來。
- values_not_served 的 PASS 不再說「/metrics 照寫送出每一個閾值值」。
- 空的 `--config-dir` 是 caller error（rc 2）。

da-guard 由 conftest 的 session fixture 以 `go build` 建出；建不起來就 fail、不 skip。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from _platform_fs import require_shebang_scripts, symlink_or_skip

import _lib_tenant_values as tv
import validate_config as vc
from test_served_values_readers_matrix import _tree

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "scripts" / "tools" / "ops"

pytestmark = pytest.mark.usefixtures("da_guard_env")

_CLEAN = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 100\n",
    "tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "50"\n',
}
_NESTED = {
    "_defaults.yaml": "defaults:\n  mysql_connections: 100\n",
    "team/_defaults.yaml": "defaults:\n  mysql_connections: 90\n",
    "team/tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "5"\n',
}
_LTE_80 = "policies:\n  - name: cap\n    target: mysql_connections\n    operator: lte\n    value: 80\n"

# check name -> how to run it: every row that reads the tenants through da-guard.
_ROWS = {
    "profiles": lambda conf_d, pol: vc.check_profiles(conf_d),
    "policy_dsl": lambda conf_d, pol: vc.check_policy_dsl(conf_d, pol),
    "values_not_served": lambda conf_d, pol: vc.check_values_not_served(conf_d),
}


def _policy(tmp_path: Path, body: str = _LTE_80) -> str:
    pol = tmp_path / "policy.yaml"   # conf.d 之外
    pol.write_text(body, encoding="utf-8")
    return str(pol)


def _script(tmp_path: Path, name: str, body: str) -> str:
    require_shebang_scripts()
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    p.chmod(0o755)
    return str(p)


def _cli(conf_d: Path, *extra: str) -> tuple[int, dict[str, dict]]:
    p = subprocess.run([sys.executable, str(OPS / "validate_config.py"), "--config-dir", str(conf_d),
                        *extra, "--json"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300, check=False)
    return p.returncode, {r["check"]: r for r in json.loads(p.stdout)}


# ── 1：da-guard 本身失敗 → caller error，提示指向 binary ─────────────────────────

def _not_executable(tmp_path: Path, da_guard: str) -> str:
    p = tmp_path / "da-guard-644"
    shutil.copy(da_guard, p)
    p.chmod(0o644)
    return str(p)


_BROKEN = {
    "not-executable": _not_executable,
    "exits-1": lambda tmp_path, _dg: _script(tmp_path, "dg-exit1", "exit 1\n"),
    "exits-0-no-json": lambda tmp_path, _dg: _script(tmp_path, "dg-true", "exit 0\n"),
}
# v2.9.x：沒有 served-values／effective，連 `-h` 都當成缺 --config-dir（exit 2）。
_OLD = 'echo "da-guard: --config-dir is required" >&2\nexit 2\n'


@pytest.mark.parametrize("row", sorted(_ROWS))
@pytest.mark.parametrize("kind", sorted(_BROKEN))
def test_a_da_guard_that_cannot_run_is_not_blamed_on_the_tree(kind, row, tmp_path, monkeypatch,
                                                               da_guard_binary):
    conf_d = str(_tree(tmp_path / "t", _CLEAN))
    monkeypatch.setenv("DA_GUARD_BINARY", _BROKEN[kind](tmp_path, da_guard_binary))
    got = _ROWS[row](conf_d, _policy(tmp_path))
    assert got["status"] == vc.FAIL and got["caller_error"] is True, got
    assert got["hint"] == vc._DA_GUARD_BROKEN_HINT, got
    assert "repair or remove the file" not in got["hint"], got


@pytest.mark.parametrize("row", sorted(_ROWS))
def test_a_da_guard_without_the_subcommand_is_named_too_old(row, tmp_path, monkeypatch):
    """#2115 D：舊版 da-guard（沒有子命令）不再被報成設定檔讀不到。"""
    conf_d = str(_tree(tmp_path / "t", _CLEAN))
    monkeypatch.setenv("DA_GUARD_BINARY", _script(tmp_path, "dg-old", _OLD))
    got = _ROWS[row](conf_d, _policy(tmp_path))
    assert got["status"] == vc.FAIL and got["caller_error"] is True, got
    assert got["hint"] == vc._DA_GUARD_TOO_OLD_HINT, got
    assert any("older than this tool" in d for d in got["details"]), got


def test_cli_exits_2_for_a_da_guard_that_cannot_run(tmp_path, monkeypatch, da_guard_binary):
    conf_d = _tree(tmp_path / "t", _CLEAN)
    monkeypatch.setenv("DA_GUARD_BINARY", _not_executable(tmp_path, da_guard_binary))
    rc, rows = _cli(conf_d, "--policy-dsl", _policy(tmp_path))
    assert rc == 2, (rc, rows["policy_dsl"])
    assert rows["policy_dsl"]["suggested_action"] == vc._DA_GUARD_BROKEN_HINT, rows["policy_dsl"]


@pytest.mark.parametrize("other", ["none", "dangling", "bad-yaml"])
def test_cli_rc_for_a_too_old_da_guard_follows_the_existing_downgrade(other, tmp_path,
                                                                      monkeypatch):
    """既有的 exit-code 規則不改：caller error 是 rc 2，但 `yaml_syntax` 列另有點名的無法使用檔案
    （其 `unusable_files`）時降為 rc 1（找不到 da-guard 也同樣降）。文件寫的就是這兩個數字。"""
    conf_d = _tree(tmp_path / "t", _CLEAN)
    if other == "dangling":
        symlink_or_skip("/nonexistent/x.yaml", conf_d / "zz.yaml")
    elif other == "bad-yaml":
        (conf_d / "bad.yaml").write_text("tenants:\n  b: [\n", encoding="utf-8")
    monkeypatch.setenv("DA_GUARD_BINARY", _script(tmp_path, "dg-old", _OLD))
    rc, rows = _cli(conf_d, "--policy-dsl", _policy(tmp_path))
    assert rows["policy_dsl"]["suggested_action"] == vc._DA_GUARD_TOO_OLD_HINT, rows["policy_dsl"]
    assert rc == (2 if other == "none" else 1), (rc, rows["policy_dsl"], rows["yaml_syntax"])


def test_a_da_guard_killed_by_a_signal_is_not_classified(tmp_path):
    """被 signal 殺掉（負的結束碼）與逾時同類：不判 broken／stale（已知限制）。"""
    killer = _script(tmp_path, "dg-killed", "kill -9 $$\n")
    with pytest.raises(tv.EffectiveError) as ei:
        tv.load_effective(tmp_path, binary=killer)
    assert ei.value.returncode is not None and ei.value.returncode < 0, ei.value.returncode
    assert not ei.value.broken and not ei.value.binary_fault


@pytest.mark.parametrize("row", sorted(_ROWS))
def test_control_a_tree_a_working_da_guard_refuses_stays_the_trees(row, tmp_path):
    """對照組：可用的 da-guard（`-h` 回 0）拒收的樹（F0，exit 2）仍是樹的問題。"""
    conf_d = str(_tree(tmp_path, {
        "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_connections_critical: 95\n",
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections_critical: 95\n",
    }))
    got = _ROWS[row](conf_d, _policy(tmp_path))
    # effective（profiles／values_not_served）不拒收 F0；served-values（policy_dsl）拒收。
    assert (got["status"] == vc.FAIL) is (row == "policy_dsl"), got
    assert got["caller_error"] is False, got
    # getattr：對照組在沒有這兩個提示的版本上也要能跑（反事實時它須綠）。
    binary_hints = (getattr(vc, "_DA_GUARD_BROKEN_HINT", None), vc._DA_GUARD_TOO_OLD_HINT)
    assert got.get("hint") is None or got["hint"] not in binary_hints, got


def test_exit_2_is_asked_of_the_binary_not_of_its_stderr(tmp_path):
    """同一段 stderr、同樣 exit 2：`-h` 回 0 的是樹的問題，回 2 的是 binary 太舊。"""
    say = "echo 'flag provided but not defined: -schedules' >&2\nexit 2\n"
    knows = _script(tmp_path, "knows", 'for a in "$@"; do [ "$a" = -h ] && exit 0; done\n' + say)
    old = _script(tmp_path, "old", say)
    for binary, stale in ((knows, False), (old, True)):
        with pytest.raises(tv.ServedValuesError) as ei:
            tv.load_served_tree(tmp_path, binary=binary, schedules=True)
        assert ei.value.stale is stale and ei.value.binary_fault is stale, binary


def test_could_not_run_is_broken(tmp_path, da_guard_binary):
    p = _not_executable(tmp_path, da_guard_binary)
    with pytest.raises(tv.EffectiveError) as ei:
        tv.load_effective(tmp_path, binary=p)
    assert ei.value.broken and ei.value.returncode is None


# ── 2：--policy-dsl 載入失敗時，同列不說規則來自它 ───────────────────────────────

@pytest.mark.parametrize("dsl", ["missing", "broken-yaml", "ok"])
def test_policy_dsl_line_names_the_flag_only_when_it_was_loaded(dsl, tmp_path):
    conf_d = str(_tree(tmp_path / "t", _NESTED))
    pol = (str(tmp_path / "missing.yaml") if dsl == "missing"
           else _policy(tmp_path, "policies: [\n" if dsl == "broken-yaml" else _LTE_80))
    row = vc._run_check("policy_dsl", vc.check_policy_dsl, conf_d, pol, _config_dir=conf_d)
    line = [d for d in row["details"] if "not read for `_policies`" in d]
    assert line, row
    if dsl == "ok":   # 對照組：真的載入了才說
        assert "rules also come from --policy-dsl" in line[0], row
    else:
        assert row["caller_error"] is True and "--policy-dsl" not in line[0], row


# ── 3：根 `_defaults.yaml` 是斷掉的 symlink ─────────────────────────────────────

@pytest.mark.parametrize("root", ["dangling", "absent"])
def test_dangling_root_carrier_is_not_called_absent(root, tmp_path):
    files = {k: v for k, v in _NESTED.items() if k != "_defaults.yaml"}
    conf_d = _tree(tmp_path / "t", files)
    if root == "dangling":
        symlink_or_skip("/nonexistent/_defaults.yaml", conf_d / "_defaults.yaml")
    row = vc._run_check("policy_dsl", vc.check_policy_dsl, str(conf_d), _policy(tmp_path),
                        _config_dir=str(conf_d))
    line = [d for d in row["details"] if "not read for `_policies`" in d]
    assert line, row
    if root == "dangling":
        assert "root defaults carrier cannot be opened" in line[0], row
        assert "there is no root defaults carrier" not in line[0], row
        assert row["hint"] != vc.POLICY_DSL_NESTED_NO_ROOT_HINT, row
    else:   # 對照組
        assert "there is no root defaults carrier" in line[0], row


# ── 4：沒有閾值被評估到時說出來；values_not_served 不說「每個值都照寫送出」──────────

@pytest.mark.parametrize("root", [True, False], ids=["root-defaults", "no-root-defaults"])
def test_pass_says_when_no_threshold_was_compared(root, tmp_path):
    files = dict(_CLEAN) if root else {"tenant-a.yaml": _CLEAN["tenant-a.yaml"]}
    conf_d = str(_tree(tmp_path / "t", files))
    served = tv.load_served_values(conf_d)["tenant-a"]
    assert ("mysql_connections" in served.unserved) is not root   # 前提：Go 的答案
    row = vc.check_policy_dsl(conf_d, _policy(tmp_path))
    assert row["status"] == vc.PASS, row   # 判定不變：閾值看 /metrics 實際送出的數字
    text = "\n".join(row["details"])
    if root:   # 對照組：只有原本那一行
        assert row["details"] == ["1 rules evaluated across 1 tenants — all passed"], row
    else:
        assert "/metrics serves no threshold value for any tenant" in text, row
        assert "1 threshold value(s) are configured but not served" in text, row
        assert "tenant-a: mysql_connections" in text, row


def test_values_not_served_pass_does_not_claim_every_value_is_served(tmp_path):
    conf_d = str(_tree(tmp_path / "t", {"tenant-a.yaml": _CLEAN["tenant-a.yaml"]}))
    assert tv.load_served_values(conf_d)["tenant-a"].unserved   # served-values：沒送
    row = vc.check_values_not_served(conf_d)
    assert row["status"] == vc.PASS, row
    assert not any("serves every threshold value" in d for d in row["details"]), row


# ── 5：空的 --config-dir 是 caller error ─────────────────────────────────────────

def test_empty_config_dir_with_policy_dsl_is_a_caller_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    row = vc.check_policy_dsl(str(empty), _policy(tmp_path))
    assert row["caller_error"] is True and row["hint"] == vc._POLICY_DSL_NO_CONFIG_FILE_HINT, row
    rc, rows = _cli(empty, "--policy-dsl", _policy(tmp_path))
    assert rc == 2, (rc, rows["policy_dsl"])
