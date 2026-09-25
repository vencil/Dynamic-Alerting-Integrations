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

三輪盲審後（owner 選項 3）：init **不判斷 exporter 能否讀某個檔**，只問「這個
檔可能提及某個要求的租戶嗎」，可能就跳過並說明怎麼驗證。「可能提及」必須涵蓋
exporter 實際讀到的宣告，這件事由 `tests/shared/init_declaration_parity_matrix.json`
回答（Go 端 `ScanDirTree`、Python 端 `_possible_mentions`）。本檔只斷言**跑完
之後 conf.d 裡有哪些檔、客戶的檔有沒有被動、說了什麼**，客戶檔的形狀取自矩陣
裡 Go 實測過的列。

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


def _place(out: Path, rel: str, body) -> Path:
    p = out / "conf.d" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
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
    # 第 1 輪 F1 的 1a（矩陣列 F1-1a-*，Go 實測 exporter 接受並宣告該租戶）
    ("1a-trailing-doc", "team.yaml",
     '---\ntenants:\n  db-c:\n    mysql_connections: "80"\n---\n', "db-c"),
    ("1a-second-doc", "team.yaml", "tenants:\n  db-c: {}\n---\nfoo: 1\n",
     "db-c"),
    ("1a-on", "team.yaml", "tenants:\n  on: {}\n", "on"),
    ("1a-007", "team.yaml", "tenants:\n  007: {}\n", "007"),
    # 第 3 輪 E2：escape 寫法的 key（矩陣列 R3-*，Go 讀成 db-c）——舊判定看
    # 不到它、另造 duplicate。
    ("escaped-key-x", "team.yaml", 'tenants:\n  "db\\x2dc": {}\n', "db-c"),
    ("escaped-key-all-hex", "team.yaml",
     'tenants:\n  "\\x64\\x62\\x2d\\x63": {}\n', "db-c"),
    ("escaped-key-multiline", "team.yaml",
     'tenants:\n  ? "db-\\\n    c"\n  : {}\n', "db-c"),
    ("utf16", "team.yaml",
     "\ufefftenants:\n  db-c: {}\n".encode("utf-16-le"), "db-c"),
    ("flow-tab", "team.yaml", "tenants: {db-c:\t{}}\n", "db-c"),
    # exporter 整檔拒收的檔也一樣跳過：init 不判斷 exporter 能否讀取，
    # 訊息叫客戶用 guard 驗證（第 1 輪 1b 的形狀）。
    ("1b-scalar-body", "team.yaml", 'tenants:\n  db-c: "oops"\n', "db-c"),
]


@pytest.mark.parametrize("case,rel,body,tenant", _SKIP_CASES,
                         ids=[c[0] for c in _SKIP_CASES])
def test_a_tenant_the_customers_file_may_declare_is_skipped(
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
    assert f"{tenant} may already be declared by conf.d/{rel}" in run.stderr, \
        run.stderr
    assert "does not check whether the exporter can read" in run.stderr
    assert f"da-tools guard defaults-impact --config-dir {conf}" in run.stderr
    # 摘要（stdout）也列出、且「下一步」不叫客戶去編輯沒產生的檔案
    assert f"conf.d/{rel}" in run.stdout
    assert f"Edit conf.d/{tenant}.yaml" not in run.stdout
    assert "Traceback" not in run.stderr


@pytest.mark.parametrize("body", [
    'tenants:\n  db-c:\n    note: "\\U00110000"\n',
    "tenants:\n  db-c:\n    note: " + "[" * 3000 + "]" * 3000 + "\n",
    "tenants:\n  !!binary ZGItYw==: {}\n",
], ids=["out-of-range-U-escape", "deep-flow-nesting", "explicit-tag"])
def test_a_file_init_cannot_read_through_counts_as_mentioning_everyone(
        tmp_path, body):
    """第 3 輪：`\\U00110000` 讓 PyYAML 丟 ValueError、深度巢狀丟
    RecursionError，明確 tag 能把任何位元組變成 key——三者都是「可能提及
    **每一個**要求的租戶」，而且不能是 traceback。

    ⚠️ 用沒有 init 自有檔的樹：在 `_brownfield` 上，「提及每一個」也包括
    已存在的 `db-a.yaml`／`db-b.yaml` 的租戶，那是並存拒絕（下方拒絕表）。"""
    out = tmp_path / "repo"
    _place(out, "team.yaml", body)
    run = _run(out, "db-c,db-d")
    assert run.returncode == 0, run.stderr[-800:]
    assert "Traceback" not in run.stderr
    for t in ("db-c", "db-d"):
        assert f"{t} may already be declared by conf.d/team.yaml" in run.stderr
        assert not (out / "conf.d" / f"{t}.yaml").exists()


def test_a_customer_file_that_mentions_no_requested_tenant_draws_no_comment(
        tmp_path):
    """init 不是 validator：沒提到要求租戶的客戶檔，壞了也不說話。"""
    out = _brownfield(tmp_path)
    _place(out, "other.yaml", 'tenants:\n  db-x: "oops"\n')
    run = _run(out, RERUN_TENANTS)
    assert run.returncode == 0, run.stderr[-800:]
    assert "other.yaml" not in run.stderr
    assert "init_project" not in run.stderr
    assert (out / "conf.d" / "db-c.yaml").is_file()


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
     ["init would overwrite conf.d/db-a.yaml", "may also declare db-c"]),
    # F4：與 init 路徑只差大小寫的既有項目（內容宣告別人，所以不會被跳過）。
    ("case-variant", {"DB-C.YAML": "tenants:\n  someone-else: {}\n"},
     ["init would write conf.d/db-c.yaml", "conf.d/DB-C.YAML",
      "differs only in case"]),
    # 具體提及（token）＋init 自有檔已存在 ⇒ 並存拒絕照舊。
    ("concrete-beside-own", {"team.yaml": "tenants:\n  db-a: {}\n"},
     ["conf.d/db-a.yaml (a path init writes) already exists",
      "conf.d/team.yaml may also declare tenant db-a"]),
    # 第 2 輪 F4：只差大小寫的是**上層目錄**（`kustomize/` 之於 `Kustomize/`）。
    ("case-variant-parent", {"../Kustomize/README.md": "theirs\n"},
     ["init would write kustomize", "Kustomize", "differs only in case"]),
]


@pytest.mark.parametrize("dry", [False, True], ids=["run", "dry-run"])
@pytest.mark.parametrize("case,files,says", _REFUSALS,
                         ids=[c[0] for c in _REFUSALS])
def test_a_tree_init_cannot_safely_extend_is_refused(
        tmp_path, case, files, says, dry):
    out = _brownfield(tmp_path)
    if case == "case-variant-parent":
        import shutil
        shutil.rmtree(out / "kustomize")
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


def test_a_file_init_cannot_read_through_does_not_block_its_own_paths(
        tmp_path):
    """並存拒絕只在**具體**提及時觸發（為了不讓 init 造出新的重複，不是替客戶
    驗證既有樹）。讀不透的檔（這裡是明確 tag）在自有檔已存在的租戶旁：init 照常
    重寫自有檔、點名那個檔；沒有自有檔的要求租戶照舊跳過。"""
    out = _brownfield(tmp_path)
    conf = out / "conf.d"
    own = conf / "db-a.yaml"
    own.write_text(own.read_text(encoding="utf-8") + "\n# HAND-EDIT\n",
                   encoding="utf-8")
    theirs = _place(out, "team.yaml", "tenants:\n  !!binary ZGItYw==: {}\n")
    before = theirs.read_bytes()

    run = _run(out, RERUN_TENANTS)

    assert run.returncode == 0, run.stderr[-800:]
    assert "HAND-EDIT" not in own.read_text(encoding="utf-8")   # 重寫了
    assert theirs.read_bytes() == before
    for t in ("db-a", "db-b"):
        assert (f"init cannot read conf.d/team.yaml through (it carries an "
                f"explicit YAML tag); if it also declares {t}, it duplicates "
                f"conf.d/{t}.yaml") in run.stderr, run.stderr
    assert "da-tools guard defaults-impact" in run.stderr
    assert "init cannot read conf.d/team.yaml through" in run.stdout
    # db-c 沒有自有檔：(d) 照舊視為可能提及 ⇒ 跳過
    assert "db-c may already be declared by conf.d/team.yaml" in run.stderr
    assert not (conf / "db-c.yaml").exists()


def test_the_clobber_refusal_does_not_claim_a_second_declaration(tmp_path):
    """F2：db-z 只被宣告一次，訊息不得說「宣告了兩次、請合併」。"""
    out = _brownfield(tmp_path)
    _place(out, "db-a.yaml", "tenants:\n  db-a: {}\n  db-z: {}\n")
    run = _run(out, "db-a,db-z")
    assert run.returncode == 1, run.stderr[-800:]
    assert "twice" not in run.stderr and "merge them" not in run.stderr
    assert "db-z would be declared nowhere" in run.stderr
    assert "leave db-a out of --tenants" in run.stderr


def test_a_case_twin_of_the_output_dirs_conf_d_is_refused(tmp_path):
    """F4：`Conf.D/` 已存在、`conf.d/` 不存在——最後一層檔名比不出來。"""
    out = tmp_path / "repo"
    theirs = out / "Conf.D" / "db-x.yaml"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("tenants:\n  db-x: {}\n", encoding="utf-8")
    before = _snapshot(out)
    run = _run(out, "db-a")
    assert run.returncode == 1, run.stderr[-800:]
    assert _snapshot(out) == before
    assert "init would write conf.d" in run.stderr and "Conf.D" in run.stderr


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
    assert "db-c may already be declared by conf.d/db-c.yml" in run.stdout
    assert "db-c may already be declared by conf.d/db-c.yml" in run.stderr


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
    assert "db-c 可能已由 conf.d/db-c.yml 宣告，未產生 conf.d/db-c.yaml" in run.stderr
