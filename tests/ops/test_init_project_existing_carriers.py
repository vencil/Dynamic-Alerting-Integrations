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

⛔ 「哪個檔宣告了誰」**不在這裡重算**。第一版在本檔自己寫了一份 `safe_load`
判定來當 oracle——那是實作的逐字複本，實作錯（#1942 盲審 F1：`on:`、`007:`、
多文件、純量 body、重複 key）時兩邊一起錯、測試照綠。那個問題的答案現在由
`tests/shared/init_declaration_parity_matrix.json` 回答：Go 端拿 exporter 的
`ScanDirTree` 驗每一列、Python 端拿 `_plan_confd` 驗同一列。本檔只斷言**跑完
之後 conf.d 裡有哪些檔、客戶的檔有沒有被動**，客戶檔的內容形狀都取自矩陣裡
已被 Go 端實測過的列。

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

from _lib_confd import iter_config_files  # noqa: E402

TOOL = Path(REPO_ROOT) / "scripts" / "tools" / "ops" / "init_project.py"
BASE_TENANTS = "db-a,db-b"
RERUN_TENANTS = "db-a,db-b,db-c"
_INIT_OWN = {"_defaults.yaml", "db-a.yaml", "db-b.yaml"}


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


def _confd_files(conf: Path) -> set:
    """conf.d 裡 exporter 會看的設定檔（相對 conf.d 的 POSIX 路徑）。"""
    return {p.relative_to(conf).as_posix() for p in iter_config_files(conf)}


def _snapshot(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def _generated_tenant(conf: Path, name: str) -> list:
    """init 自己產生的 `<t>.yaml` 宣告的租戶（init 的輸出，不是客戶檔）。"""
    return list(yaml.safe_load((conf / name).read_text(encoding="utf-8"))
                ["tenants"])


def _brownfield(tmp_path: Path) -> Path:
    """一棵既有 repo：跑過一次 init 再刪掉 marker（＝從沒跑過 init 的 repo）。"""
    out = tmp_path / "repo"
    first = _run(out, BASE_TENANTS)
    assert first.returncode == 0, first.stderr[-800:]
    (out / ".da-init.yaml").unlink()
    return out


def _place(out: Path, rel: str, body: str) -> Path:
    p = out / "conf.d" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8", newline="\n")
    return p


_ONE = 'tenants:\n  db-c:\n    mysql_connections: "5"\n'
_TWO = ('tenants:\n  db-c:\n    mysql_connections: "5"\n'
        '  db-d:\n    mysql_connections: "6"\n')

# (情境, 客戶檔相對 conf.d 的路徑, 內容, 被跳過的租戶)
_SKIP_CASES = [
    ("N", "db-c.yml", _ONE, "db-c"),
    ("M", "team.yaml", _TWO, "db-c"),
    ("C", "DB-C.YAML", _ONE, "db-c"),
    ("S", "prod/db-c.yaml", _ONE, "db-c"),
    # #1942 盲審 F1 的 1a：舊判定讀不到這些宣告、另造了 duplicate（矩陣列
    # F1-1a-*，Go 端實測 exporter 接受且宣告該租戶）。
    ("1a-trailing-doc", "team.yaml",
     '---\ntenants:\n  db-c:\n    mysql_connections: "80"\n---\n', "db-c"),
    ("1a-second-doc", "team.yaml", "tenants:\n  db-c: {}\n---\nfoo: 1\n",
     "db-c"),
    ("1a-on", "team.yaml", "tenants:\n  on: {}\n", "on"),
    ("1a-007", "team.yaml", "tenants:\n  007: {}\n", "007"),
]


@pytest.mark.parametrize("case,rel,body,tenant", _SKIP_CASES,
                         ids=[c[0] for c in _SKIP_CASES])
def test_a_tenant_declared_by_the_customers_file_is_skipped(
        tmp_path, case, rel, body, tenant):
    out = _brownfield(tmp_path)
    conf = out / "conf.d"
    theirs = _place(out, rel, body)
    before = theirs.read_bytes()

    run = _run(out, f"{BASE_TENANTS},{tenant}")

    assert run.returncode == 0, run.stderr[-800:]
    # init 沒有為它另造載體：conf.d 只有 init 自己的三個檔＋客戶那一個
    assert _confd_files(conf) == _INIT_OWN | {rel}
    assert theirs.read_bytes() == before
    for own in ("db-a.yaml", "db-b.yaml"):
        assert _generated_tenant(conf, own) == [own[:-5]]
    assert f"{tenant} is already declared by conf.d/{rel}" in run.stderr, \
        run.stderr
    # 摘要（stdout）也列出、且「下一步」不叫客戶去編輯沒產生的檔案
    assert f"conf.d/{rel}" in run.stdout
    assert f"Edit conf.d/{tenant}.yaml" not in run.stdout


def test_a_customer_defaults_carrier_in_another_spelling_is_kept(tmp_path):
    """情境 D：客戶的根 defaults 是 `_defaults.yml`。"""
    out = _brownfield(tmp_path)
    conf = out / "conf.d"
    (conf / "_defaults.yaml").rename(conf / "_defaults.yml")
    before = (conf / "_defaults.yml").read_bytes()

    run = _run(out, RERUN_TENANTS)

    assert run.returncode == 0, run.stderr[-800:]
    assert _confd_files(conf) == {"_defaults.yml", "db-a.yaml", "db-b.yaml",
                                  "db-c.yaml"}
    assert (conf / "_defaults.yml").read_bytes() == before
    assert ("platform defaults are already carried by conf.d/_defaults.yml"
            in run.stderr), run.stderr
    assert "Edit conf.d/_defaults.yml" in run.stdout


_REFUSALS = [
    ("tenant", {"db-c.yaml": _ONE, "db-c.yml": _ONE}, ["conf.d/db-c.yml"]),
    ("defaults", {"_defaults.yml": "defaults: {}\n"}, ["conf.d/_defaults.yml"]),
    # F2：客戶的 db-a.yaml 同時宣告這次要求的 db-c——跳過 db-c 又覆寫
    # db-a.yaml 會讓 db-c 無處宣告。
    ("clobber", {"db-a.yaml": 'tenants:\n  db-a: {}\n  db-c:\n    x: "5"\n'},
     ["init would overwrite conf.d/db-a.yaml", "also declares db-c"]),
    # F1 的 1b：exporter 整檔拒收（矩陣列 F1-1b-*），舊判定當成已宣告而跳過
    # ⇒ db-c 從此無處宣告。
    ("1b-scalar-body", {"team.yaml": 'tenants:\n  db-c: "oops"\n'},
     ["conf.d/team.yaml names db-c", "the exporter rejects this file"]),
    ("1b-duplicate-key", {"team.yaml": "tenants:\n  db-c: {}\n  db-c: {}\n"},
     ["conf.d/team.yaml names db-c", "defined twice"]),
    ("syntax-error", {"team.yaml": "tenants:\n  db-c: [unclosed\n"},
     ["conf.d/team.yaml names db-c", "not valid YAML"]),
    # F4：與 init 路徑只差大小寫的既有項目（內容宣告別人，所以不會被跳過）。
    ("case-variant", {"DB-C.YAML": "tenants:\n  someone-else: {}\n"},
     ["init would write conf.d/db-c.yaml", "conf.d/DB-C.YAML",
      "differs only in case"]),
]


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry-run"])
@pytest.mark.parametrize("case,files,says", _REFUSALS,
                         ids=[c[0] for c in _REFUSALS])
def test_a_tree_init_cannot_safely_extend_is_refused(
        tmp_path, case, files, says, dry):
    out = _brownfield(tmp_path)
    for rel, body in files.items():
        _place(out, rel, body)
    before = _snapshot(out)

    run = _run(out, RERUN_TENANTS, *(["--dry-run"] if dry else []))

    assert run.returncode == 1, (run.returncode, run.stderr[-800:])
    assert _snapshot(out) == before, "拒絕時不得寫入任何檔案"
    assert not (out / ".da-init.yaml").exists()
    assert "init refused" in run.stderr
    for phrase in says:
        assert phrase in run.stderr, run.stderr


def test_the_clobber_refusal_does_not_claim_a_second_declaration(tmp_path):
    """F2：db-z 只被宣告一次，訊息不得說「宣告了兩次、請合併」。"""
    out = _brownfield(tmp_path)
    _place(out, "db-a.yaml", "tenants:\n  db-a: {}\n  db-z: {}\n")
    run = _run(out, "db-a,db-z")
    assert run.returncode == 1, run.stderr[-800:]
    assert "twice" not in run.stderr and "merge them" not in run.stderr
    assert "db-z would be declared nowhere" in run.stderr
    assert "leave db-a out of --tenants" in run.stderr


def test_a_rejected_file_naming_no_requested_tenant_only_warns(tmp_path):
    out = _brownfield(tmp_path)
    _place(out, "other.yaml", 'tenants:\n  db-x: "oops"\n')
    run = _run(out, RERUN_TENANTS)
    assert run.returncode == 0, run.stderr[-800:]
    assert ("conf.d/other.yaml: the exporter rejects this file" in run.stderr
            and "this run is unaffected" in run.stderr), run.stderr
    assert (out / "conf.d" / "db-c.yaml").is_file()


def test_dry_run_leaves_the_skipped_carrier_out_and_says_why(tmp_path):
    out = _brownfield(tmp_path)
    _place(out, "db-c.yml", _ONE)
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
    assert _confd_files(conf) == {"_defaults.yaml", "db-a.yaml", "db-b.yaml",
                                  "db-c.yaml"}
    for t in ("db-a", "db-b", "db-c"):
        assert _generated_tenant(conf, f"{t}.yaml") == [t]
    assert "NOTE: init_project" not in run.stderr
    assert "WARN: init_project" not in run.stderr
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
    _place(out, "db-c.yml", _ONE)
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
    _place(out, "db-c.yml", _ONE)
    run = _run(out, RERUN_TENANTS, "--deploy", "kustomize")
    assert run.returncode == 0, run.stderr[-800:]
    kust = yaml.safe_load((out / "kustomize" / "base" / "kustomization.yaml")
                          .read_text(encoding="utf-8"))
    files = kust["configMapGenerator"][0]["files"]
    assert "db-c.yml" in files and "db-c.yaml" not in files
    assert "not in the kustomize ConfigMap" not in run.stderr
    marker = yaml.safe_load((out / ".da-init.yaml").read_text(encoding="utf-8"))
    assert marker["tenants"] == ["db-a", "db-b", "db-c"]
    assert marker["tenants_declared_by_existing_files"] == {
        "db-c": ["conf.d/db-c.yml"]}


@pytest.mark.parametrize("deploy", ["kustomize", "helm"])
def test_a_skipped_tenant_below_the_root_is_named_for_kustomize(tmp_path,
                                                                deploy):
    """F3：`configMapGenerator.files` 是扁平的，子目錄載體進不了 ConfigMap。"""
    out = _brownfield(tmp_path)
    _place(out, "prod/db-c.yaml", _ONE)
    run = _run(out, RERUN_TENANTS, "--deploy", deploy)
    assert run.returncode == 0, run.stderr[-800:]
    says = ("db-c's carrier conf.d/prod/db-c.yaml is below the conf.d root, "
            "so it is not in the kustomize ConfigMap")
    if deploy == "kustomize":
        assert says in run.stderr, run.stderr
        assert says in run.stdout
        assert "gitops-deployment.md §3" in run.stderr
    else:
        # helm 不經 configMapGenerator：不該對它說 kustomize 的事
        assert says not in run.stderr
    assert not (out / "conf.d" / "db-c.yaml").exists()


def test_helm_values_do_not_skeleton_a_skipped_tenant(tmp_path):
    out = _brownfield(tmp_path)
    _place(out, "db-c.yml", _ONE)
    run = _run(out, RERUN_TENANTS, "--deploy", "helm")
    assert run.returncode == 0, run.stderr[-800:]
    text = (out / "environments" / "prod" / "values.yaml").read_text(
        encoding="utf-8")
    assert "conf.d/db-c.yaml" not in text
    assert "#   db-c: carry across from conf.d/db-c.yml" in text
    assert "#   db-a:" in text
    # 檔頭的填寫指示：刪掉 `tenants:` 後的 `{}`，再逐行刪掉 `# `（井號＋一個空白）
    _, _, block = text.partition("thresholdConfig:")
    lines = ["thresholdConfig:"] + [
        ln.replace("# ", "", 1) if ln.startswith("  #") else ln
        for ln in block.replace("tenants: {}", "tenants:").splitlines()]
    tenants = yaml.safe_load("\n".join(lines))["thresholdConfig"]["tenants"]
    assert set(tenants) == {"db-a", "db-b"}


def test_the_notices_follow_the_cli_language(tmp_path):
    out = _brownfield(tmp_path)
    _place(out, "db-c.yml", _ONE)
    run = _run(out, RERUN_TENANTS, lang="zh_TW.UTF-8")
    assert run.returncode == 0, run.stderr[-800:]
    assert "db-c 已由 conf.d/db-c.yml 宣告，未產生 conf.d/db-c.yaml" in run.stderr
