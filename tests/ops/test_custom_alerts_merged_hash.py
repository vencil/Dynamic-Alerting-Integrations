"""#1549：`_custom_alerts` 不再讓 describe_tenant 的 merged_hash 與 Go 不同，純搬檔不再是爆炸半徑。

樹（每支測試在 tmp_path 重建）：`_defaults.yaml` 有 `defaults: {cpu: 70}` 與頂層
`_custom_alerts` 一條 `plat_cpu`；t1 寫 cpu "80" 並有自己的 `_custom_alerts`（own_q）；
t2 只寫 cpu "60"，繼承 plat_cpu。`moved` 是同一棵樹、t1.yaml 原封不動搬到 sub/moved.yaml；
`ctl` 是沒有任何 `_custom_alerts` 的對照樹。

釘住的形狀：
- describe_tenant `--all` 的 merged_hash 等於 `da-guard effective` 的（t1、t2；ctl 也是）；
- a → moved：blast_radius `affected_tenants: 0`；
- 必響對照：plat_cpu 門檻 90 → 95，t1、t2 都報 `_custom_alerts` 變更（Go 的 merged_hash
  兩個都不動——這正是 recipe 變更要另外比的原因）；
- da-guard 不可用：describe 照常輸出、merged_hash 為 null 並附原因，tenant-verify rc 1，
  blast_radius 退回逐欄比對、純搬檔仍是 0。

在舊碼（main e6ffa663）上：hash 那支紅（describe 自算 t1 cfd03c07… ≠ Go dab72c53…）、
搬檔那支 affected 1、必響那支在「Go 的 merged_hash 不動」前提紅（舊碼印的是會動的自算
值）、不可用那支沒有 WARN；ctl 與設定值那兩支是對照組，新舊都綠。另以突變驗過：讓
blast_radius 在 hash 相同時直接跳過 ⇒ 必響那支紅；比對時保留 origin ⇒ 搬檔與不可用兩支紅。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import _lib_tenant_values as tv

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
VERIFY = REPO_ROOT / "scripts" / "tools" / "dx" / "tenant_verify.py"
BLAST = REPO_ROOT / "scripts" / "tools" / "ops" / "blast_radius.py"

_PLAT = ('  - {recipe: threshold, name: plat_cpu, metric: node_cpu, op: ">", '
         'window: 5m, threshold: "%s:warning"}\n')
_T1 = ('tenants:\n  t1:\n    cpu: "80"\n    _custom_alerts:\n'
       '      - {recipe: threshold, name: own_q, metric: queue_depth, op: ">", '
       'window: 5m, threshold: "100:warning"}\n')
_T2 = 'tenants:\n  t2:\n    cpu: "60"\n'


def _tree(root: Path, *, plat: str | None = "90", t1_at: str | None = "t1.yaml") -> Path:
    root.mkdir(parents=True)
    defaults = "defaults:\n  cpu: 70\n"
    if plat is not None:
        defaults += "_custom_alerts:\n" + _PLAT % plat
    (root / "_defaults.yaml").write_text(defaults, encoding="utf-8")
    if t1_at is not None:
        (root / t1_at).parent.mkdir(parents=True, exist_ok=True)
        (root / t1_at).write_text(_T1, encoding="utf-8")
    (root / "t2.yaml").write_text(_T2, encoding="utf-8")
    return root


def _describe_all(conf_d: Path, out: Path) -> subprocess.CompletedProcess:
    p = subprocess.run([sys.executable, str(DESCRIBE), "--all", "--conf-d", str(conf_d),
                        "--output", str(out)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return p


def _blast(base: Path, pr: Path) -> tuple[dict, str]:
    p = subprocess.run([sys.executable, str(BLAST), "--base", str(base), "--pr", str(pr)],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout), p.stderr


def _fields(report: dict) -> dict[str, list[str]]:
    return {t["tenant_id"]: sorted(e["field"] for tier in t["tiers"].values() for e in tier)
            for t in report["tenants"]}


def test_describe_merged_hash_is_da_guards(tmp_path, da_guard_env):
    conf_d = _tree(tmp_path / "a")
    _describe_all(conf_d, tmp_path / "a.json")
    got = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    go = tv.load_effective(conf_d)
    assert {t: got[t]["merged_hash"] for t in got} == {t: e.merged_hash for t, e in go.items()}
    assert set(got) == {"t1", "t2"}
    # 不空洞：describe 顯示的 effective_config 仍是編譯器 UNION（Go 的沒有 plat_cpu），
    # 所以對它自己算 hash 會與 Go 不同——這正是舊碼的 merged_hash。
    assert [a["name"] for a in got["t2"]["effective_config"]["_custom_alerts"]] == ["plat_cpu"]
    assert "_custom_alerts" not in go["t2"].effective_config
    assert "merged_hash_error" not in got["t1"]


def test_tree_without_custom_alerts_is_unchanged(tmp_path, da_guard_env):
    """對照組 ctl：沒有 `_custom_alerts` 時，Go 的值與 describe 自己的 canonical hash 相同。"""
    sys.path.insert(0, str(DESCRIBE.parent))
    import describe_tenant as dt
    conf_d = _tree(tmp_path / "ctl", plat=None, t1_at=None)
    _describe_all(conf_d, tmp_path / "ctl.json")
    got = json.loads((tmp_path / "ctl.json").read_text(encoding="utf-8"))
    own = dt._canonical_hash(dt.ConfDScanner(conf_d).effective_config("t2"))
    assert got["t2"]["merged_hash"] == tv.load_effective(conf_d)["t2"].merged_hash == own


def test_pure_file_move_is_not_a_blast_radius(tmp_path, da_guard_env):
    a = _tree(tmp_path / "a")
    moved = _tree(tmp_path / "moved", t1_at="sub/moved.yaml")
    assert (a / "t1.yaml").read_bytes() == (moved / "sub" / "moved.yaml").read_bytes()
    _describe_all(a, tmp_path / "a.json")
    _describe_all(moved, tmp_path / "moved.json")
    # 前提：describe 照常顯示 origin（provenance），且兩邊確實不同。
    origins = [json.loads((tmp_path / f).read_text(encoding="utf-8"))["t1"]["effective_config"]
               ["_custom_alerts_resolution"] for f in ("a.json", "moved.json")]
    assert origins[0] != origins[1]
    report, _ = _blast(tmp_path / "a.json", tmp_path / "moved.json")
    assert report["summary"]["affected_tenants"] == 0, report


def test_platform_recipe_retune_still_rings_for_every_inheritor(tmp_path, da_guard_env):
    """必響對照：平台 recipe 門檻 90 → 95。t1（自己也有 recipe）與 t2 的 Go merged_hash
    都不動，但兩個都要被報出 `_custom_alerts` 變更。"""
    a = _tree(tmp_path / "a")
    retuned = _tree(tmp_path / "retuned", plat="95")
    _describe_all(a, tmp_path / "a.json")
    _describe_all(retuned, tmp_path / "retuned.json")
    before = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    after = json.loads((tmp_path / "retuned.json").read_text(encoding="utf-8"))
    assert all(before[t]["merged_hash"] == after[t]["merged_hash"] for t in ("t1", "t2"))
    report, _ = _blast(tmp_path / "a.json", tmp_path / "retuned.json")
    assert report["summary"]["affected_tenants"] == 2, report
    assert {t["tenant_id"]: t["highest_tier"] for t in report["tenants"]} == {"t1": "A", "t2": "A"}
    assert _fields(report) == {"t1": ["_custom_alerts"], "t2": ["_custom_alerts"]}


def test_a_value_change_rings_through_merged_hash(tmp_path, da_guard_env):
    """設定值那半的必響：t2 的 cpu 60 → 61，只有 t2、欄位 cpu。"""
    a = _tree(tmp_path / "a")
    b = _tree(tmp_path / "b")
    (b / "t2.yaml").write_text(_T2.replace('"60"', '"61"'), encoding="utf-8")
    _describe_all(a, tmp_path / "a.json")
    _describe_all(b, tmp_path / "b.json")
    report, _ = _blast(tmp_path / "a.json", tmp_path / "b.json")
    assert _fields(report) == {"t2": ["cpu"]}, report


def test_without_da_guard(tmp_path, monkeypatch):
    """da-guard 不可用：describe 照常輸出，merged_hash 為 null 並附原因（不輸出 Python 自算值）；
    tenant-verify 回 1（不是 rollback checklist 讀成 mismatch 的 2）；blast_radius 說明它在
    逐欄比對，純搬檔仍是 0。"""
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    a = _tree(tmp_path / "a")
    moved = _tree(tmp_path / "moved", t1_at="sub/moved.yaml")
    p = _describe_all(a, tmp_path / "a.json")
    assert "WARN: merged_hash is not reported" in p.stderr, p.stderr
    got = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
    for t in ("t1", "t2"):
        assert got[t]["merged_hash"] is None, got[t]
        assert "da-guard binary not found" in got[t]["merged_hash_error"]
        assert got[t]["effective_config"]["cpu"]
    v = subprocess.run([sys.executable, str(VERIFY), "t1", "--conf-d", str(a), "--json"],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert v.returncode == 1, (v.returncode, v.stdout, v.stderr)
    assert json.loads(v.stdout)["error"] == "merged_hash_unavailable"
    _describe_all(moved, tmp_path / "moved.json")
    report, err = _blast(tmp_path / "a.json", tmp_path / "moved.json")
    assert report["summary"]["affected_tenants"] == 0, report
    assert "have no merged_hash in the input" in err, err
