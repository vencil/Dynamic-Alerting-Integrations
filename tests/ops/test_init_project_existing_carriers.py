#!/usr/bin/env python3
"""#1942：`da-tools init` 在既有 conf.d 上不得另造同一租戶的第二個載體。

缺陷形狀（main b5534de1 實測）：`run_init` 無條件寫 `conf.d/_defaults.yaml`
與每一份 `conf.d/<tenant>.yaml`，唯一閘門是 `.da-init.yaml` marker——從沒跑過
init 的 repo 沒有它，於是不帶 `--force` 也照寫。客戶已有 `db-c.yml`、
`DB-C.YAML` 或宣告 db-c 的多租戶檔時，init 再寫一份 `db-c.yaml`，rc 0、stderr
零行，而 exporter 以 `duplicate tenant ID "db-c"` 拒收整棵樹；客戶的根 defaults
是 `_defaults.yml` 時，init 寫出的 `_defaults.yaml` 讓客戶那份靜默失效。

裁決（owner，方案 C）：init 只擁有它自己的路徑（conf.d 根目錄的
`<tenant>.yaml` 與 `_defaults.yaml`）；已由其他載體宣告的租戶／defaults 跳過並
說明（rc 0）；init 自己的路徑已與其他載體並存 ⇒ 拒絕、rc 1、一個檔都不寫。

⛔ 斷言一律**依內容**：「宣告了誰」讀檔案 `tenants:` 的 key（exporter 取租戶
id 的地方），不看檔名。只數檔名的斷言在多租戶檔情境（M）下會把一棵被 exporter
拒收的樹判成綠。

⚠️ 全部走 subprocess 跑真正的 CLI：要釘的是 `main()` 這一層（拒絕發生在任何
寫入之前、rc、stderr），直接呼叫 `run_init` 會繞過它。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

from _lib_confd import (  # noqa: E402
    is_defaults_name,
    is_reserved_name,
    iter_config_files,
)

TOOL = Path(REPO_ROOT) / "scripts" / "tools" / "ops" / "init_project.py"
BASE_TENANTS = "db-a,db-b"
RERUN_TENANTS = "db-a,db-b,db-c"


def _run(out: Path, tenants: str, *extra: str, lang: str = "en_US.UTF-8"):
    # 語系傳給子行程（它自己重讀環境變數），DA_LANG 優先序最高要清掉。
    env = dict(os.environ, LANG=lang, PYTHONUTF8="1",
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("DA_LANG", None)
    env.pop("LC_ALL", None)
    return subprocess.run(
        [sys.executable, str(TOOL), "--non-interactive", "--tenants", tenants,
         "--rule-packs", "mariadb", "-o", str(out), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300, env=env)


def _declarers(conf: Path) -> dict:
    """租戶 id -> 宣告它的檔案（相對 conf.d 的 POSIX 路徑）。

    與 exporter 同一套規則：遞迴、兩種副檔名任意大小寫、`_` 開頭不是租戶
    載體、YAML 讀不進來的不算宣告。
    """
    out: dict = {}
    for p in iter_config_files(conf):
        if is_reserved_name(p.name):
            continue
        try:
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        block = doc.get("tenants") if isinstance(doc, dict) else None
        if isinstance(block, dict):
            for tid in block:
                out.setdefault(str(tid), []).append(
                    p.relative_to(conf).as_posix())
    return out


def _root_defaults(conf: Path) -> list:
    return sorted(p.name for p in iter_config_files(conf, recursive=False)
                  if is_defaults_name(p.name))


def _snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def _brownfield(tmp_path: Path) -> Path:
    """一棵既有 repo：跑過一次 init 再刪掉 marker（＝從沒跑過 init 的 repo）。"""
    out = tmp_path / "repo"
    first = _run(out, BASE_TENANTS)
    assert first.returncode == 0, first.stderr[-800:]
    (out / ".da-init.yaml").unlink()
    return out


_ONE = 'tenants:\n  db-c:\n    mysql_connections: "5"\n'
_TWO = ('tenants:\n  db-c:\n    mysql_connections: "5"\n'
        '  db-d:\n    mysql_connections: "6"\n')

# (情境, 客戶檔相對 conf.d 的路徑, 內容, 預期被跳過的租戶)
_SKIP_CASES = [
    ("N", "db-c.yml", _ONE, ["db-c"]),
    ("M", "team.yaml", _TWO, ["db-c"]),
    ("C", "DB-C.YAML", _ONE, ["db-c"]),
    ("S", "prod/db-c.yaml", _ONE, ["db-c"]),
]


@pytest.mark.parametrize("case,rel,body,skipped", _SKIP_CASES,
                         ids=[c[0] for c in _SKIP_CASES])
def test_a_tenant_declared_by_the_customers_file_is_skipped(
        tmp_path, case, rel, body, skipped):
    out = _brownfield(tmp_path)
    conf = out / "conf.d"
    theirs = conf / rel
    theirs.parent.mkdir(parents=True, exist_ok=True)
    theirs.write_text(body, encoding="utf-8", newline="\n")
    before = theirs.read_bytes()

    run = _run(out, RERUN_TENANTS)

    assert run.returncode == 0, run.stderr[-800:]
    decl = _declarers(conf)
    wanted = {"db-a", "db-b", "db-c"} | set(yaml.safe_load(body)["tenants"])
    dupes = {t: decl.get(t) for t in wanted if len(decl.get(t, [])) != 1}
    assert not dupes, f"每個租戶應恰由一個檔案宣告：{dupes}"
    assert decl["db-c"] == [rel]
    assert theirs.read_bytes() == before
    for t in skipped:
        assert f"{t} is already declared by conf.d/{rel}" in run.stderr, \
            run.stderr
    # 摘要（stdout）也列出、且「下一步」不叫客戶去編輯沒產生的檔案
    assert f"conf.d/{rel}" in run.stdout
    assert "Edit conf.d/db-c.yaml" not in run.stdout


def test_a_customer_defaults_carrier_in_another_spelling_is_kept(tmp_path):
    """情境 D：客戶的根 defaults 是 `_defaults.yml`。"""
    out = _brownfield(tmp_path)
    conf = out / "conf.d"
    (conf / "_defaults.yaml").rename(conf / "_defaults.yml")
    before = (conf / "_defaults.yml").read_bytes()

    run = _run(out, RERUN_TENANTS)

    assert run.returncode == 0, run.stderr[-800:]
    assert _root_defaults(conf) == ["_defaults.yml"]
    assert (conf / "_defaults.yml").read_bytes() == before
    assert ("platform defaults are already carried by conf.d/_defaults.yml"
            in run.stderr), run.stderr
    assert "Edit conf.d/_defaults.yml" in run.stdout
    decl = _declarers(conf)
    assert all(len(decl[t]) == 1 for t in ("db-a", "db-b", "db-c")), decl


_REFUSALS = [
    ("tenant", {"conf.d/db-c.yaml": _ONE, "conf.d/db-c.yml": _ONE}),
    ("defaults", {"conf.d/_defaults.yml": "defaults: {}\n"}),
    # 客戶的 db-a.yaml 同時宣告 db-c：跳過 db-c 又覆寫 db-a.yaml 會讓 db-c
    # 從此無處宣告——自相矛盾，所以拒絕。
    ("clobber", {"conf.d/db-a.yaml":
                 'tenants:\n  db-a: {}\n  db-c:\n    mysql_connections: "5"\n'}),
]


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry-run"])
@pytest.mark.parametrize("case,files", _REFUSALS, ids=[c[0] for c in _REFUSALS])
def test_inits_own_path_beside_another_carrier_is_refused(
        tmp_path, case, files, dry):
    out = _brownfield(tmp_path)
    for rel, body in files.items():
        (out / rel).write_text(body, encoding="utf-8", newline="\n")
    before = _snapshot(out)

    run = _run(out, RERUN_TENANTS, *(["--dry-run"] if dry else []))

    assert run.returncode == 1, (run.returncode, run.stderr[-800:])
    assert _snapshot(out) == before, "拒絕時不得寫入任何檔案"
    assert not (out / ".da-init.yaml").exists()
    assert "init refused" in run.stderr
    for rel in files:
        assert rel in run.stderr, run.stderr


def test_dry_run_leaves_the_skipped_carrier_out_and_says_why(tmp_path):
    out = _brownfield(tmp_path)
    (out / "conf.d" / "db-c.yml").write_text(_ONE, encoding="utf-8",
                                              newline="\n")
    before = _snapshot(out)

    run = _run(out, RERUN_TENANTS, "--dry-run")

    assert run.returncode == 0, run.stderr[-800:]
    assert _snapshot(out) == before
    listed = [ln.strip().split(" ")[0] for ln in run.stdout.splitlines()
              if ln.startswith("  conf.d/")]
    assert "conf.d/db-c.yaml" not in listed and "conf.d/db-a.yaml" in listed
    assert "db-c is already declared by conf.d/db-c.yml" in run.stdout
    assert "db-c is already declared by conf.d/db-c.yml" in run.stderr


def test_fresh_tree_is_unchanged(tmp_path):
    """對照組：全新目錄，每個租戶一份 init 自己的 `<t>.yaml`，沒有任何說明。"""
    out = tmp_path / "fresh"
    run = _run(out, RERUN_TENANTS)
    assert run.returncode == 0, run.stderr[-800:]
    conf = out / "conf.d"
    assert _declarers(conf) == {t: [f"{t}.yaml"]
                                for t in ("db-a", "db-b", "db-c")}
    assert _root_defaults(conf) == ["_defaults.yaml"]
    assert "NOTE: init_project" not in run.stderr
    marker = yaml.safe_load((out / ".da-init.yaml").read_text(encoding="utf-8"))
    assert marker["tenants"] == ["db-a", "db-b", "db-c"]
    assert "tenants_declared_by_existing_files" not in marker


def test_force_still_regenerates_inits_own_paths_but_not_the_customers(tmp_path):
    out = tmp_path / "repo"
    assert _run(out, "db-a").returncode == 0
    conf = out / "conf.d"
    own = [conf / "_defaults.yaml", conf / "db-a.yaml"]
    for f in own:
        f.write_text(f.read_text(encoding="utf-8") + "\n# HAND-EDIT\n",
                     encoding="utf-8")
    (conf / "db-c.yml").write_text(_ONE, encoding="utf-8", newline="\n")
    theirs = (conf / "db-c.yml").read_bytes()

    run = _run(out, "db-a,db-c", "--force")

    assert run.returncode == 0, run.stderr[-800:]
    for f in own:
        assert "HAND-EDIT" not in f.read_text(encoding="utf-8"), f.name
    assert (conf / "db-c.yml").read_bytes() == theirs
    assert not (conf / "db-c.yaml").exists()


def test_the_deploy_artifacts_name_the_customers_carrier(tmp_path):
    """kustomize 的 `files:` 列客戶的載體、不列沒寫的；marker 記下誰是客戶的。"""
    out = _brownfield(tmp_path)
    (out / "conf.d" / "db-c.yml").write_text(_ONE, encoding="utf-8",
                                              newline="\n")
    run = _run(out, RERUN_TENANTS, "--deploy", "kustomize")
    assert run.returncode == 0, run.stderr[-800:]
    kust = yaml.safe_load((out / "kustomize" / "base" / "kustomization.yaml")
                          .read_text(encoding="utf-8"))
    files = kust["configMapGenerator"][0]["files"]
    assert "db-c.yml" in files and "db-c.yaml" not in files
    marker = yaml.safe_load((out / ".da-init.yaml").read_text(encoding="utf-8"))
    assert marker["tenants"] == ["db-a", "db-b", "db-c"]
    assert marker["tenants_declared_by_existing_files"] == {
        "db-c": ["conf.d/db-c.yml"]}


def test_helm_values_do_not_skeleton_a_skipped_tenant(tmp_path):
    out = _brownfield(tmp_path)
    (out / "conf.d" / "db-c.yml").write_text(_ONE, encoding="utf-8",
                                              newline="\n")
    run = _run(out, RERUN_TENANTS, "--deploy", "helm")
    assert run.returncode == 0, run.stderr[-800:]
    text = (out / "environments" / "prod" / "values.yaml").read_text(
        encoding="utf-8")
    assert "conf.d/db-c.yaml" not in text
    assert "#   db-c: carry across from conf.d/db-c.yml" in text
    assert "#   db-a:" in text
    # 照檔頭指示取消註解後，db-c 不會變成一筆帶著虛構 key 的租戶
    # 檔頭的填寫指示：刪掉 `tenants:` 後的 `{}`，再逐行刪掉 `# `（井號＋一個空白）
    _, _, block = text.partition("thresholdConfig:")
    lines = ["thresholdConfig:"] + [
        ln.replace("# ", "", 1) if ln.startswith("  #") else ln
        for ln in block.replace("tenants: {}", "tenants:").splitlines()]
    tenants = yaml.safe_load("\n".join(lines))["thresholdConfig"]["tenants"]
    assert set(tenants) == {"db-a", "db-b"}


def test_an_unparseable_customer_file_is_named_and_declares_nothing(tmp_path):
    out = _brownfield(tmp_path)
    broken = out / "conf.d" / "db-c.yml"
    broken.write_text("tenants: [unclosed\n", encoding="utf-8", newline="\n")
    run = _run(out, RERUN_TENANTS)
    assert run.returncode == 0, run.stderr[-800:]
    assert "conf.d/db-c.yml does not parse as YAML" in run.stderr
    assert (out / "conf.d" / "db-c.yaml").is_file()


def test_the_notices_follow_the_cli_language(tmp_path):
    out = _brownfield(tmp_path)
    (out / "conf.d" / "db-c.yml").write_text(_ONE, encoding="utf-8",
                                              newline="\n")
    run = _run(out, RERUN_TENANTS, lang="zh_TW.UTF-8")
    assert run.returncode == 0, run.stderr[-800:]
    assert "db-c 已由 conf.d/db-c.yml 宣告，未產生 conf.d/db-c.yaml" in run.stderr
