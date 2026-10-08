"""`describe_tenant --what-if <副本> --replaces <樹內檔>`：以副本內容取代 conf.d 裡那個檔，
再重新求值（#2097 項 1；盲審 F1 依 owner 裁決採選項 (b)）。

oracle 是 Go：`da-guard effective` 對「替換後的樹」求出的 merged_hash。每格先把新內容
寫成樹外的副本、對原樹跑 CLI（`--what-if 副本 --replaces 樹內檔`），再把同一份內容寫進
樹裡那個路徑讓 da-guard 讀。所以：

- what_if_merged_hash 要等於 Go 對替換後的樹的 merged_hash；
- baseline_merged_hash 要等於 Go 對原樹的 merged_hash；
- would_trigger_reload 要等於兩個 Go hash 是否不同。

⛔ 為什麼不能再拿樹內檔本身當 `--what-if`（F1）：scanner 的 baseline 與 what-if 讀的是
同一份 bytes，原地改值等於拿檔案跟自己比，一律報「不 reload」（fail-open）。舊版本檔
`test_cli_unchanged_root_platform_file_reports_no_reload` 正是那個形狀——內容不變所以
不 reload，是套套邏輯，什麼都抓不到；它已換成「副本未改 → 不 reload／副本改值 → reload」
兩向都有的格子。現在沒帶 `--replaces` 卻指向樹內既有檔 → rc 2。
同檔判斷以 inode 為準（`os.path.samefile`）：hard link 解析後路徑不同、卻是同一個檔
（盲審第 2 輪 D）。`--replaces` 沒帶 `--what-if` 在任何模式（含 `--diff` / `--all`）都是 rc 2（E）。

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
from _platform_fs import symlink_or_skip

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
    # chain carriers: F1 fail-open 在 main 上就有（原地改值報 substitute、不 reload）。
    ("chain-carrier-changed", "sub/_defaults.yaml",
     "defaults:\n  container_cpu: 66\n", False, "substitute", True),
    ("root-carrier-unchanged", "_defaults.yaml", None, False, "substitute", False),
]


def _go_hash(conf_d: Path, tid: str = "t1") -> str:
    return tv.load_effective(conf_d)[tid].merged_hash


def test_matrix_is_not_vacuous():
    assert {c[5] for c in CASES} == {True, False}
    assert len({c[0] for c in CASES}) == len(CASES)


def _cli(conf_d: Path, what_if: Path, *extra: str, tid: str = "t1") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DESCRIBE), tid, "--conf-d", str(conf_d), "--what-if", str(what_if),
         *extra, "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})


@pytest.mark.parametrize("name,rel,new,elect,stype,reload", CASES, ids=[c[0] for c in CASES])
def test_replaces_matches_go_on_the_substituted_tree(tmp_path, name, rel, new, elect, stype, reload):
    conf_d = _tree(tmp_path, elect)
    target = conf_d / rel
    copy = tmp_path / "edited" / Path(rel).name
    copy.parent.mkdir()
    copy.write_text(target.read_text(encoding="utf-8") if new is None else new, encoding="utf-8")
    go_before = _go_hash(conf_d)
    p = _cli(conf_d, copy, "--replaces", str(target))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    # 讓 Go 讀「替換後的樹」：把副本內容寫進樹裡那個路徑。
    target.write_text(copy.read_text(encoding="utf-8"), encoding="utf-8")
    go_after = _go_hash(conf_d)
    assert (go_before != go_after) is reload, (name, "前提：Go 的答案")
    assert out["substitution_type"] == stype, out
    assert out["replaces"] == str(target.resolve()), out
    assert out["what_if_file"] == str(copy.resolve()), out
    assert out["baseline_merged_hash"] == go_before, out
    assert out["what_if_merged_hash"] == go_after, out
    assert out["would_trigger_reload"] is reload and out["merged_hash_changed"] is reload, out


@pytest.mark.parametrize("rel", ["_profiles.yaml", "_defaults.yaml", "sub/_defaults.yaml",
                                 "sub/t1.yaml"])
def test_in_tree_file_without_replaces_is_refused(tmp_path, rel):
    """F1：樹內既有檔（含 chain 檔）直接當 `--what-if` → rc 2，並示範 `--replaces` 的用法。
    修正前 chain 檔回 `substitute`、根平台檔也是，原地改值都報不 reload。"""
    conf_d = _tree(tmp_path, True)
    target = conf_d / rel
    p = _cli(conf_d, target)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "compare the file with itself" in p.stderr, p.stderr
    assert f"--what-if <copy> --replaces {target.resolve()}" in p.stderr, p.stderr


def test_link_to_an_in_tree_file_without_replaces_is_refused(tmp_path):
    """以解析後的路徑判斷：指向樹內檔的連結也是同一份 bytes。"""
    conf_d = _tree(tmp_path, True)
    link = tmp_path / "link.yaml"
    symlink_or_skip(conf_d / "sub" / "_defaults.yaml", link)
    p = _cli(conf_d, link)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "--replaces" in p.stderr, p.stderr


def test_replaces_with_the_same_file_is_refused(tmp_path):
    conf_d = _tree(tmp_path, True)
    target = conf_d / "_profiles.yaml"
    p = _cli(conf_d, target, "--replaces", str(target))
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "same file" in p.stderr and "copy" in p.stderr, p.stderr


def test_replaces_through_a_link_to_the_what_if_file_is_refused(tmp_path):
    """同檔判斷用解析後的路徑：`--replaces` 經連結指回 `--what-if` 檔也一樣拒收。"""
    conf_d = _tree(tmp_path, True)
    target = conf_d / "_profiles.yaml"
    link = tmp_path / "p.yaml"
    symlink_or_skip(target, link)
    p = _cli(conf_d, link, "--replaces", str(target))
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert "same file" in p.stderr, p.stderr


def _hard_link(src: Path, dst: Path) -> None:
    try:
        os.link(src, dst)
    except (OSError, NotImplementedError) as exc:  # 不支援 hard link 的檔案系統
        pytest.skip(f"hard link unavailable: {exc}")


def test_hard_link_to_an_in_tree_file_without_replaces_is_refused(tmp_path):
    """盲審第 2 輪 D：hard link 解析後的路徑不同、卻是同一個檔——以 inode 判斷，rc 2。
    修正前回 append-external、would_trigger_reload=False，而 Go 的 hash 已經變了。"""
    conf_d = _tree(tmp_path, True)
    target = conf_d / "sub" / "_defaults.yaml"
    link = tmp_path / "hard.yaml"
    _hard_link(target, link)
    p = _cli(conf_d, link)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "through a hard link" in p.stderr, p.stderr
    assert f"--replaces {target.resolve()}" in p.stderr, p.stderr


def test_replaces_through_a_hard_link_to_the_what_if_file_is_refused(tmp_path):
    """盲審第 2 輪 D：`--what-if` 是 `--replaces` 的 hard link → 同一個檔，rc 2。"""
    conf_d = _tree(tmp_path, True)
    target = conf_d / "sub" / "_defaults.yaml"
    link = tmp_path / "hard.yaml"
    _hard_link(target, link)
    p = _cli(conf_d, link, "--replaces", str(target))
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "same file" in p.stderr, p.stderr


@pytest.mark.parametrize("where", ["outside", "missing", "hidden-in-tree"])
def test_replaces_must_name_a_listed_file(tmp_path, where):
    conf_d = _tree(tmp_path, True)
    copy = tmp_path / "copy.yaml"
    copy.write_text(_PROFILES, encoding="utf-8")
    target = {"outside": tmp_path / "elsewhere.yaml",
              "missing": conf_d / "nope.yaml",
              "hidden-in-tree": conf_d / "sub" / ".draft.yaml"}[where]
    if where != "missing":
        target.write_text(_PROFILES, encoding="utf-8")
    p = _cli(conf_d, copy, "--replaces", str(target))
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert f"--replaces {target.resolve()} is not a file of the scanned tree" in p.stderr, p.stderr


@pytest.mark.parametrize("mode", [[], ["--diff", "t2"], ["--all"]],
                         ids=["default", "diff", "all"])
def test_replaces_requires_what_if(tmp_path, mode):
    """盲審第 2 輪 E：`--diff` / `--all` 分支先於這條檢查，修正前 rc 0 且 `--replaces` 被吞掉。"""
    conf_d = _tree(tmp_path, True)
    tid = [] if mode == ["--all"] else ["t1"]
    p = subprocess.run(
        [sys.executable, str(DESCRIBE), *tid, "--conf-d", str(conf_d), *mode,
         "--replaces", str(conf_d / "_profiles.yaml")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "--replaces requires --what-if" in p.stderr, p.stderr


@pytest.mark.parametrize("mode", [["--what-if", "COPY"], ["--all"]], ids=["what-if", "all"])
def test_empty_replaces_is_refused_not_taken_as_absent(tmp_path, mode):
    """盲審第 3 輪 B3-8：`--replaces ''` 原本以 truthiness 判斷被當成「沒給」——`--all` rc 0、
    `--what-if` 走 append-external rc 0。現在 rc 2。"""
    conf_d = _tree(tmp_path, True)
    copy = tmp_path / "copy.yaml"
    copy.write_text("defaults:\n  pg_connections: 41\n", encoding="utf-8")
    args = [str(copy) if a == "COPY" else a for a in mode]
    tid = [] if mode == ["--all"] else ["t1"]
    p = subprocess.run(
        [sys.executable, str(DESCRIBE), *tid, "--conf-d", str(conf_d), *args, "--replaces", ""],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    want = "--replaces requires --what-if" if mode == ["--all"] else "--replaces needs a path"
    assert want in p.stderr, p.stderr


@pytest.mark.parametrize("extra", [[], ["--replaces", "X"]], ids=["alone", "with-replaces"])
def test_empty_what_if_is_refused_not_taken_as_absent(tmp_path, extra):
    """盲審第 4 輪 R4-2：`--what-if ''` 原本以 truthiness 判斷——單獨給 rc 0 被靜默忽略，
    配 `--replaces` 則被誤報成「--replaces requires --what-if」。現在具名 rc 2。"""
    conf_d = _tree(tmp_path, True)
    p = subprocess.run(
        [sys.executable, str(DESCRIBE), "t1", "--conf-d", str(conf_d), "--what-if", "", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert p.returncode == 2, (p.returncode, p.stdout, p.stderr)
    assert p.stdout == "", p.stdout
    assert "--what-if needs a path (got an empty string)" in p.stderr, p.stderr
    assert "--replaces requires --what-if" not in p.stderr, p.stderr


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
    copy = tmp_path / "t1.yaml"
    copy.write_text("tenants:\n  t9: {}\n", encoding="utf-8")
    with pytest.raises(dt.WhatIfError, match="does not declare 't1'"):
        dt.what_if_result(scanner, "t1", copy.resolve(), copy,
                          dt._load_yaml(copy), dt._load_platform_doc(copy),
                          replaces=target.resolve())
    p = _cli(conf_d, copy, "--replaces", str(target))
    assert p.returncode == 2 and "does not declare 't1'" in p.stderr, (p.returncode, p.stderr)
