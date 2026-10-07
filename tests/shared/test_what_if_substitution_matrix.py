"""`describe_tenant --what-if <檔>`：以這份內容取代 conf.d 裡同路徑的檔，再重新求值（#2097 項 1）。

oracle 是 Go：`da-guard effective` 對「替換後的樹」求出的 merged_hash。做法是先讓
`ConfDScanner` 讀原樹（baseline），再把新內容寫進樹裡同一個路徑、把 what-if 指向它
——那正是「以這份內容取代該檔」——然後讓 da-guard 讀寫完的樹。所以：

- what_if_merged_hash 要等於 Go 對替換後的樹的 merged_hash；
- baseline_merged_hash 要等於 Go 對原樹的 merged_hash；
- would_trigger_reload 要等於兩個 Go hash 是否不同。

修正前（main 59e58c81）：不在租戶 defaults chain 上的樹內檔（根 `_profiles.yaml`、別的分支的
`_defaults.yaml`、租戶自己的檔）一律當成新插入的 chain 層、整份內容當 defaults 合併——
內容不變也報 `insert` + reload。

樹外的檔（`append-external`）與樹內不存在於 listing 的路徑（`insert`）維持原行為，各有一格
對照組。

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

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
import describe_tenant as dt  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")

_ROOT_DEFAULTS = "defaults:\n  mysql_connections: 80\n  container_cpu: 70\n  pg_connections: 40\n"
_PROFILES = "profiles:\n  p1:\n    container_cpu: 20\n"


def _tree(root: Path, elect: bool) -> Path:
    conf_d = root / "conf.d"
    files = {
        "_defaults.yaml": _ROOT_DEFAULTS,
        "_profiles.yaml": _PROFILES,
        "sub/_defaults.yaml": "defaults:\n  container_cpu: 65\n",
        "sub/t1.yaml": "tenants:\n  t1:\n    mysql_connections: 50\n"
                       + ("    _profile: p1\n" if elect else ""),
        "other/_defaults.yaml": "defaults:\n  container_cpu: 30\n",
        "other/t2.yaml": "tenants:\n  t2: {}\n",
    }
    for rel, body in files.items():
        p = conf_d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return conf_d


# (name, file, new content or None for unchanged, tenant elects p1,
#  expected substitution_type, expected reload)
CASES = [
    ("profiles-unchanged-noelect", "_profiles.yaml", None, False, "substitute", False),
    ("profiles-unchanged-elect", "_profiles.yaml", None, True, "substitute", False),
    ("profiles-elected-value-changed", "_profiles.yaml",
     "profiles:\n  p1:\n    container_cpu: 25\n", True, "substitute", True),
    ("profiles-changed-not-elected", "_profiles.yaml",
     "profiles:\n  p1:\n    container_cpu: 25\n", False, "substitute", False),
    ("profiles-file-gains-tenants-block", "_profiles.yaml",
     _PROFILES + "tenants:\n  t1:\n    pg_connections: 7\n", False, "substitute", True),
    ("off-chain-carrier-changed", "other/_defaults.yaml",
     "defaults:\n  container_cpu: 31\n  mysql_connections: 1\n", True, "substitute", False),
    ("own-tenant-file-changed", "sub/t1.yaml",
     "tenants:\n  t1:\n    mysql_connections: 55\n    _profile: p1\n", True, "substitute", True),
    ("own-tenant-file-unchanged", "sub/t1.yaml", None, True, "substitute", False),
    ("other-tenant-file-changed", "other/t2.yaml",
     "tenants:\n  t2:\n    mysql_connections: 3\n", True, "substitute", False),
    # must-ring control: a chain carrier (the pre-#2097 `substitute` path).
    ("CONTROL-chain-carrier-changed", "sub/_defaults.yaml",
     "defaults:\n  container_cpu: 66\n", False, "substitute", True),
    ("CONTROL-root-carrier-unchanged", "_defaults.yaml", None, False, "substitute", False),
]


def _go_hash(conf_d: Path, tid: str = "t1") -> str:
    return tv.load_effective(conf_d)[tid].merged_hash


def test_matrix_is_not_vacuous():
    assert {c[5] for c in CASES} == {True, False}
    assert len({c[0] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("name,rel,new,elect,stype,reload", CASES, ids=[c[0] for c in CASES])
def test_what_if_matches_go_on_the_substituted_tree(tmp_path, name, rel, new, elect, stype, reload):
    conf_d = _tree(tmp_path, elect)
    go_before = _go_hash(conf_d)
    scanner = dt.ConfDScanner(conf_d)
    target = conf_d / rel
    if new is not None:
        target.write_text(new, encoding="utf-8")
    go_after = _go_hash(conf_d)
    assert (go_before != go_after) is reload, (name, "前提：Go 的答案")
    out = dt.what_if_result(scanner, "t1", target.resolve(), target,
                            dt._load_yaml(target), dt._load_platform_doc(target))
    assert out["substitution_type"] == stype, out
    assert out["baseline_merged_hash"] == go_before, out
    assert out["what_if_merged_hash"] == go_after, out
    assert out["would_trigger_reload"] is reload and out["merged_hash_changed"] is reload, out


def _cli(conf_d: Path, what_if: Path, tid: str = "t1") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DESCRIBE), tid, "--conf-d", str(conf_d), "--what-if", str(what_if),
         "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


@pytest.mark.parametrize("elect", [False, True])
def test_cli_unchanged_root_platform_file_reports_no_reload(tmp_path, elect):
    """issue 原文的那條指令：檔案內容不變，不得報 reload。"""
    conf_d = _tree(tmp_path, elect)
    p = _cli(conf_d, conf_d / "_profiles.yaml")
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert (out["substitution_type"], out["would_trigger_reload"]) == ("substitute", False), out
    assert out["what_if_merged_hash"] == _go_hash(conf_d)
    assert (out["added_keys"], out["removed_keys"], out["changed_keys"]) == ({}, {}, {}), out


def test_cli_outside_the_tree_still_appends(tmp_path):
    """對照組：樹外的檔維持 append-external（整份當最高層 defaults）。"""
    conf_d = _tree(tmp_path, False)
    outside = tmp_path / "whatif.yaml"
    outside.write_text("defaults:\n  pg_connections: 41\n", encoding="utf-8")
    p = _cli(conf_d, outside)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert (out["substitution_type"], out["would_trigger_reload"]) == ("append-external", True), out
    assert out["changed_keys"] == {"pg_connections": {"baseline": 40, "what_if": 41}}, out


def test_cli_unlisted_path_inside_the_tree_still_inserts(tmp_path):
    """對照組：樹內但不在 listing 的路徑（隱藏檔，walker 不讀）維持 insert。"""
    conf_d = _tree(tmp_path, False)
    hidden = conf_d / "sub" / ".draft.yaml"
    hidden.write_text("defaults:\n  pg_connections: 42\n", encoding="utf-8")
    p = _cli(conf_d, hidden)
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["substitution_type"] == "insert", out


def test_own_tenant_file_that_no_longer_declares_the_tenant_is_refused(tmp_path):
    conf_d = _tree(tmp_path, False)
    scanner = dt.ConfDScanner(conf_d)
    target = conf_d / "sub" / "t1.yaml"
    target.write_text("tenants:\n  t9: {}\n", encoding="utf-8")
    with pytest.raises(dt.WhatIfError, match="does not declare 't1'"):
        dt.what_if_result(scanner, "t1", target.resolve(), target,
                          dt._load_yaml(target), dt._load_platform_doc(target))
