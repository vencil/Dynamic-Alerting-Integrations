"""寫 conf.d 的工具：判定取自 parse-tree、寫入前以 parse-tree 確認（ADR-036 §2，#2906 E2）。

每支工具最多六列（工具形狀用不到的那一列在下方寫明為什麼沒有）：

1. **相符就寫**：產生的文字讀回與預期相同 ⇒ 寫入。
2. **讀回不同就不寫**：PyYAML 改寫時換了寫法（`!!float 1` → `1.0`）、或把字串寫成
   yaml.v3 讀成數字的樣子（`1e3`）⇒ 拒寫、一個位元組都不動、結束碼照工具的約定。
3. **原檔讀不了就不寫**：parse-tree 讀不了的原檔（重複鍵）⇒ 拒寫、具名。
4. **沒有讀取端的路徑不寫**：隱藏路徑、目錄沒選上的 `_defaults` 拼法 ⇒ 拒寫。
5. **沒有 da-guard ⇒ exit 2**（threshold_govern 的函式層是 `ParseTreeError`，main 的
   decorator 給 2）。
6. **判定是 parse-tree 的**：一個 yaml.v3 與 PyYAML 讀法不同的輸入，工具給的是 yaml.v3
   的判定。

各工具既有測試檔裡同性質的列（例如 patch_config 的 TestDecisionsAreParseTrees、
deprecate_rule 的封閉清單表）照留，這裡是逐工具、同一張表的那一份。

沒有的列：patch_config 的第 4 列——它只寫 ConfigMap 裡宣告租戶的 key 或新建
`<tenant>.yaml`，名字先經 `is_carrier_candidate`（YAML 副檔名、非隱藏、非 `_`），平面
目錄裡這樣的名字一定有讀取端；da_assembler 的第 3、4 列——它從 CR 整份重新產生檔案、
不以舊檔為基礎（舊檔不讀），檔名是 DNS-1123 名稱加 `.yaml`，不會是隱藏或 `_` 名；
init_project／scaffold_tenant 的第 3、4 列——只寫新產生的 `_defaults.yaml` 與
`<tenant>.yaml`（init 的既有檔規則見 tests/ops/test_init_project_existing_carriers.py）。
onboard_platform／migrate_rule／generate_tenant_fixture（#2906 F1）同屬「產生新檔」：
沒有第 3 列（不讀原檔）；第 4 列只有 onboard_platform 有——它的檔名是租戶 id，
CLI 先以 ADR-035 擋下不合法的 id，函式層呼叫則由確認擋下；migrate_rule 的兩個檔名
固定，generate_tenant_fixture 的檔名是 `<domain>-<db>-<NNNN>.yaml` 與
`_defaults.yaml`。第 6 列對產生新檔的工具就是第 2 列：預期內容是工具要寫的資料，
判定是 yaml.v3 讀回的型別。

Tenant ids are fixture names (CLAUDE.md #9).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
for _p in (TOOLS, TOOLS / "ops", TOOLS / "dx", REPO / "tests" / "ops"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _lib_parse_tree  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _tree(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def _bytes(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _run(script: str, *argv: str, env=None, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOLS / script), *argv], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=180, cwd=cwd,
        env={**(os.environ if env is None else env), "PYTHONIOENCODING": "utf-8"})


def _no_da_guard(tmp_path: Path) -> dict:
    return {**os.environ, "DA_GUARD_BINARY": str(tmp_path / "no-such-da-guard")}


# ── deprecate_rule ────────────────────────────────────────────────────────

_DR_DEFAULTS = "defaults:\n  cpu_usage: 80\n  mem_usage: 90\n"
_DR_TENANT = "tenants:\n  tenant-a:\n    cpu_usage: '85'\n"


def _dr(root, *extra, env=None):
    return _run("ops/deprecate_rule.py", "cpu_usage", "--config-dir", str(root),
                "--execute", *extra, env=env)


def test_deprecate_rule_writes_what_reads_back(tmp_path):
    root = _tree(tmp_path / "conf.d", {"_defaults.yaml": _DR_DEFAULTS,
                                       "tenant-a.yaml": _DR_TENANT})
    r = _dr(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cpu_usage" not in (root / "_defaults.yaml").read_text(encoding="utf-8")
    assert "cpu_usage" not in (root / "tenant-a.yaml").read_text(encoding="utf-8")


def test_deprecate_rule_refuses_a_rewrite_that_reads_back_differently(tmp_path):
    # `!!float 1` is dumped `1.0`: another source text — and the tenant file,
    # fine on its own, is not written either (all files or none).
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  cpu_usage: 80\n  mem_usage: !!float 1\n",
        "tenant-a.yaml": _DR_TENANT})
    before = _bytes(root)
    r = _dr(root)
    assert r.returncode == 1, r.stdout + r.stderr
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "content_differs" in tail and "defaults.mem_usage" in tail, tail
    assert _bytes(root) == before


def test_deprecate_rule_does_not_write_an_unreadable_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": _DR_DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu_usage: '85'\n    cpu_usage: '86'\n"})
    before = (root / "tenant-a.yaml").read_bytes()
    r = _dr(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "duplicate_key" in r.stdout.split("下架未完成", 1)[1]
    assert (root / "tenant-a.yaml").read_bytes() == before


def test_deprecate_rule_does_not_write_a_spelling_no_reader_reads(tmp_path):
    root = _tree(tmp_path / "conf.d", {"_defaults.yaml": _DR_DEFAULTS,
                                       "_defaults.yml": "defaults:\n  cpu_usage: 70\n"})
    before = (root / "_defaults.yml").read_bytes()
    r = _dr(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "_defaults.yml" in r.stdout.split("下架未完成", 1)[1]
    assert (root / "_defaults.yml").read_bytes() == before


def test_deprecate_rule_without_da_guard_is_exit_2(tmp_path):
    root = _tree(tmp_path / "conf.d", {"_defaults.yaml": _DR_DEFAULTS})
    before = _bytes(root)
    r = _dr(root, env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert _bytes(root) == before


def test_deprecate_rule_judges_a_carrier_by_yaml_v3(tmp_path):
    # `0o17` is an int to yaml.v3 (the exporter decodes it into float64) and a
    # string to PyYAML: read the PyYAML way the carrier was "dropped" and the
    # run blocked; read through parse-tree it is a number — rc 0, kept as written.
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  cpu_usage: 80\n  mem_usage: 0o17\n"})
    r = _dr(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "mem_usage: 0o17" in (root / "_defaults.yaml").read_text(encoding="utf-8")


# ── patch_config ──────────────────────────────────────────────────────────

def _pc(cm_data, *argv):
    import patch_config as pc
    from _patch_config_fake import FakeCluster
    cluster = FakeCluster(cm_data)
    with mock.patch("patch_config.run_cmd", side_effect=cluster), \
            mock.patch("sys.argv", ["patch_config.py", "--json", *argv,
                                    "--poll-interval", "0.001"]), \
            mock.patch("sys.stdout") as out:
        try:
            pc.main()
            rc = 0
        except SystemExit as exc:
            rc = exc.code
    doc = json.loads("".join(c.args[0] for c in out.write.call_args_list))
    return rc, doc, cluster


_PC_DEFAULTS = "defaults:\n  cpu: 70\n"


def test_patch_config_writes_what_reads_back():
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n    mem: ~\n"},
                     "tenant-a", "cpu", "6")
    assert rc == 0, doc
    assert len(c.patches) == 1
    # an untouched `~` is written back as `~` (text production only)
    assert "mem: ~" in c.data["tenant-a.yaml"], c.data


def test_patch_config_refuses_a_rewrite_that_reads_back_differently():
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n    mem: !!float 1\n"},
                     "tenant-a", "cpu", "6")
    assert (rc, doc["status"], doc["reason"]) == (2, "caller_error", "write_refused"), doc
    assert c.patches == []


def test_patch_config_does_not_write_an_unreadable_key():
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n    cpu: '6'\n"},
                     "tenant-a", "cpu", "7")
    assert (rc, doc["reason"]) == (2, "configmap_shape"), doc
    assert c.patches == []


def test_patch_config_without_da_guard_is_exit_2(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n"},
                     "tenant-a", "cpu", "6")
    assert (rc, doc["reason"]) == (2, "da_guard_unavailable"), doc
    assert c.patches == []


def test_patch_config_locates_the_tenant_yaml_v3_reads():
    # `on:` is the string key "on" to yaml.v3 (True to PyYAML's typing). The
    # fake cluster's toy exporter reads with PyYAML, so this row stops at the
    # patch apply would send.
    import patch_config as pc
    cm = {"data": {"_defaults.yaml": _PC_DEFAULTS,
                   "team.yaml": "tenants:\n  on:\n    cpu: '5'\n"}}
    patch = pc.build_patch(cm, "multi-file", "on", "cpu", "6")
    assert patch == {"data": {"team.yaml": "tenants:\n  on:\n    cpu: '6'\n"}}, patch


# ── da_assembler ──────────────────────────────────────────────────────────

def _cr(tenants, defaults=None):
    spec = {"tenants": tenants}
    if defaults is not None:
        spec["defaults"] = defaults
    return {"kind": "ThresholdConfig", "metadata": {"name": "ok", "namespace": "ns"},
            "spec": spec}


def test_da_assembler_writes_what_reads_back(tmp_path):
    import da_assembler
    da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}, {"cpu": "1e3"}),
                               tmp_path, cli=True)
    assert "cpu: 1e3" in (tmp_path / "ok.yaml").read_text(encoding="utf-8")


def test_da_assembler_refuses_a_value_the_dump_retypes(tmp_path):
    import _lib_write_check
    import da_assembler
    # the JSON string "1e3" is dumped plain, a float to yaml.v3
    with pytest.raises(_lib_write_check.WriteRefused) as exc:
        da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "1e3"}}), tmp_path, cli=True)
    assert "tenants.tenant-a.cpu: expected !!str '1e3', read !!float '1e3'" in str(exc.value)
    assert list(tmp_path.iterdir()) == []


def test_da_assembler_without_da_guard_is_a_parse_tree_error(tmp_path, monkeypatch):
    import da_assembler
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    out = tmp_path / "out"
    with pytest.raises(_lib_parse_tree.ParseTreeError):
        da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}), out, cli=True)
    assert not out.exists()


def test_da_assembler_judges_defaults_by_yaml_v3(tmp_path):
    import _lib_write_check
    import da_assembler
    # `0o17` (a JSON string) is a number to yaml.v3: written; `0o8` is not.
    da_assembler.reconcile_one(_cr({"tenant-a": {}}, {"cpu": "0o17"}), tmp_path, cli=True)
    with pytest.raises(_lib_write_check.WriteRefused):
        da_assembler.reconcile_one(
            {**_cr({"tenant-a": {}}, {"cpu": "0o8"}), "metadata": {"name": "ok2"}},
            tmp_path, cli=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ok.yaml"]


# ── init_project ──────────────────────────────────────────────────────────

def _init_config(tenants):
    import init_project as ip
    return {"ci": "github", "deploy": "kustomize", "rule_packs": ["mariadb"],
            "tenants": tenants, "namespace": "monitoring",
            "da_tools_image": ip.DA_TOOLS_IMAGE}


def test_init_project_writes_what_reads_back(tmp_path):
    r = _run("ops/init_project.py", "--ci", "github", "--tenants", "tenant-a",
             "--non-interactive", "-o", str(tmp_path / "repo"))
    assert r.returncode == 0, r.stderr[-800:]
    assert (tmp_path / "repo" / "conf.d" / "tenant-a.yaml").is_file()


def test_init_project_refuses_a_generated_text_that_reads_back_differently(
        tmp_path, monkeypatch):
    import init_project as ip
    real = ip._tenant_override_rows
    monkeypatch.setattr(ip, "_tenant_override_rows",
                        lambda t, packs: {**real(t, packs), "probe_key": "1e3"})
    out = tmp_path / "repo"
    with pytest.raises(ip._ConfdWriteRefused) as exc:
        ip.run_init(_init_config(["tenant-a"]), str(out))
    assert "probe_key" in str(exc.value)
    assert not out.exists() or _bytes(out) == {}


def test_init_project_without_da_guard_is_exit_2(tmp_path):
    out = _tree(tmp_path / "repo", {"conf.d/team.yaml": "tenants:\n  tenant-b: {}\n"})
    before = _bytes(out)
    r = _run("ops/init_project.py", "--ci", "github", "--tenants", "tenant-a",
             "--non-interactive", "-o", str(out), env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stderr[-800:]
    assert _bytes(out) == before


def test_init_project_reads_declarations_the_yaml_v3_way(tmp_path):
    # TAB after the colon: PyYAML refuses the file (so init used to treat it
    # as naming everyone), yaml.v3 reads it — it declares tenant-b only.
    import init_project as ip
    _tree(tmp_path, {"conf.d/team.yaml": "tenants:\n  tenant-b:\t{}\n"})
    plan = ip._plan_confd(_init_config(["tenant-a", "tenant-b"]), str(tmp_path))
    assert plan.generate == ["tenant-a"] and plan.skipped == {
        "tenant-b": ["conf.d/team.yaml"]}, plan
    assert not plan.blanket


# ── scaffold_tenant ───────────────────────────────────────────────────────

def test_scaffold_tenant_writes_what_reads_back(tmp_path):
    r = _run("ops/scaffold_tenant.py", "--tenant", "tenant-a", "--db", "mariadb",
             "-o", str(tmp_path / "out"), "--non-interactive")
    assert r.returncode == 0, r.stdout[-400:] + r.stderr[-800:]
    assert (tmp_path / "out" / "tenant-a.yaml").is_file()


def test_scaffold_tenant_refuses_a_generated_text_that_reads_back_differently(tmp_path):
    import scaffold_tenant as st
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        st.write_outputs(str(out), "tenant-a", {"defaults": {"cpu": 70}},
                         {"tenants": {"tenant-a": {"cpu": "1e3"}}}, "report")
    assert exc.value.code == 2
    assert _bytes(out) == {}


def test_scaffold_tenant_without_da_guard_is_exit_2(tmp_path, monkeypatch):
    import scaffold_tenant as st
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        st.write_outputs(str(out), "tenant-a", {"defaults": {"cpu": 70}},
                         {"tenants": {"tenant-a": {"cpu": "70"}}}, "report")
    assert exc.value.code == 2
    assert _bytes(out) == {}


# ── threshold_govern (the pre-PUT check) ─────────────────────────────────

def test_threshold_govern_passes_an_edit_that_reads_back(tmp_path):
    import threshold_govern as tg
    old = 'tenants:\n  tenant-a:\n    k1: "10"\n    k2: 0o17\n'
    new = 'tenants:\n  tenant-a:\n    k1: "15"\n    k2: 0o17\n'
    assert tg.verify_only_changed(old, new, "tenant-a", {"k1": '"15"'},
                                  config_dir=str(tmp_path)) is None


def test_threshold_govern_refuses_a_value_of_another_type(tmp_path):
    # `k1: 15` reads as an int: not the string "15" the plan writes. The
    # PyYAML-era compare normalised both to "15" and let it be PUT.
    import threshold_govern as tg
    old = 'tenants:\n  tenant-a:\n    k1: "10"\n'
    new = 'tenants:\n  tenant-a:\n    k1: 15\n'
    err = tg.verify_only_changed(old, new, "tenant-a", {"k1": '"15"'},
                                 config_dir=str(tmp_path))
    assert err and "expected !!str '15', read !!int '15'" in err, err


def test_threshold_govern_refuses_an_unreadable_original_and_an_unread_path(tmp_path):
    import threshold_govern as tg
    dup = 'tenants:\n  tenant-a:\n    k1: "10"\n    k1: "11"\n'
    err = tg.verify_only_changed(dup, dup, "tenant-a", {"k1": '"15"'},
                                 config_dir=str(tmp_path))
    assert err and "original_unreadable" in err, err
    ok = 'tenants:\n  tenant-a:\n    k1: "10"\n'
    err = tg.verify_only_changed(ok, ok.replace("10", "15"), "tenant-a", {"k1": '"15"'},
                                 config_dir=str(tmp_path), rel=".tenant-a.yaml")
    assert err and "path_not_read" in err, err


def test_threshold_govern_without_da_guard_raises(tmp_path, monkeypatch):
    import threshold_govern as tg
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    ok = 'tenants:\n  tenant-a:\n    k1: "10"\n'
    with pytest.raises(_lib_parse_tree.ParseTreeError):
        tg.verify_only_changed(ok, ok, "tenant-a", {"k1": '"10"'}, config_dir=str(tmp_path))


# ── migrate_conf_d ────────────────────────────────────────────────────────

def _mcd(root, *extra, env=None):
    return _run("dx/migrate_conf_d.py", "--conf-d", str(root), "--dry-run", *extra, env=env)


def test_migrate_conf_d_plans_a_move_that_reads_back(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: finance\n"})
    r = _mcd(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "finance/tenant-a.yaml" in r.stdout


def test_migrate_conf_d_refuses_a_destination_no_reader_reads(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: .hidden\n"})
    before = _bytes(root)
    r = _run("dx/migrate_conf_d.py", "--conf-d", str(root), "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing to move" in r.stderr and "path_not_read" in r.stderr
    assert _bytes(root) == before


def test_migrate_conf_d_refuses_an_unreadable_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a: {}\n  tenant-a: {}\n"})
    r = _mcd(root)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "tenant-a.yaml" in r.stderr and "duplicate_key" in r.stderr


def test_migrate_conf_d_without_da_guard_is_exit_2(tmp_path):
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    r = _mcd(root, env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr


def test_migrate_conf_d_places_by_yaml_v3(tmp_path):
    # `domain: yes` is the string "yes" (PyYAML: True, unplaceable).
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: yes\n"})
    r = _mcd(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "yes/tenant-a.yaml" in r.stdout


# ── offboard_tenant (it deletes: the decision is parse-tree's) ───────────

def _ob(root, *extra, env=None):
    return _run("ops/offboard_tenant.py", "tenant-a", "--config-dir", str(root),
                "--execute", "--skip-state-check", *extra, env=env)


def test_offboard_tenant_deletes_a_file_that_declares_only_the_tenant(tmp_path):
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "tenant-a.yaml").exists()


def test_offboard_tenant_does_not_delete_a_file_that_declares_another(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a: {}\n  tenant-b: {}\n"})
    r = _ob(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tenant-b" in r.stdout and (root / "tenant-a.yaml").exists()


def test_offboard_tenant_does_not_delete_beside_an_unreadable_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
        "other.yaml": "tenants:\n  other: {}\n  other: {}\n"})
    r = _ob(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "other.yaml" in r.stdout and "duplicate_key" in r.stdout
    assert (root / "tenant-a.yaml").exists()


def test_offboard_tenant_without_da_guard_is_exit_2(tmp_path):
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    r = _ob(root, env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert (root / "tenant-a.yaml").exists()


def test_offboard_tenant_reads_the_declaration_the_yaml_v3_way(tmp_path):
    # TAB after the colon: PyYAML cannot read the file (it was "unreadable",
    # a blocker); yaml.v3 reads it — it declares tenant-a, so it is deleted.
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a:\t{}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (root / "tenant-a.yaml").exists()


def test_offboard_tenant_finds_but_does_not_delete_its_own_unreadable_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a: {}\n  tenant-a: {}\n"})
    r = _ob(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "✅ 設定檔案" in r.stdout and "duplicate_key" in r.stdout, r.stdout
    assert (root / "tenant-a.yaml").exists()


# ── E2 review follow-ups ─────────────────────────────────────────────────

def test_scaffold_generate_profile_is_confirmed_before_it_is_written(tmp_path, monkeypatch):
    # `_profiles.yaml` is a conf.d file: a profile text that does not read
    # back as generated is not written (here a key string PyYAML writes
    # plain and yaml.v3 reads as a number).
    import scaffold_tenant as st
    real = st.generate_profile
    monkeypatch.setattr(st, "generate_profile", lambda name, dbs, tier="prod": {
        "profiles": {name: {**real(name, dbs, tier=tier)["profiles"][name],
                            "probe_key": "1e3"}}})
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["scaffold_tenant.py", "--generate-profile", "p1",
                                      "--db", "mariadb", "-o", str(out)])
    with pytest.raises(SystemExit) as exc:
        st.main()
    assert exc.value.code == 2
    assert _bytes(out) == {}


def test_scaffold_from_onboard_writes_all_tenants_or_none(tmp_path):
    hints = tmp_path / "hints.json"
    hints.write_text(json.dumps({
        "tenants": ["db-a", "db-b"],
        "routing_hints": {"db-a": {"receiver_type": "webhook", "group_wait": "30s"},
                          "db-b": {"receiver_type": "webhook", "group_wait": "1e3"}},
    }), encoding="utf-8")
    out = tmp_path / "out"
    r = _run("ops/scaffold_tenant.py", "--from-onboard", str(hints), "-o", str(out))
    assert r.returncode == 2, r.stdout[-400:] + r.stderr[-800:]
    assert "db-b.yaml" in r.stderr and "group_wait" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}, sorted(_bytes(out))


def test_migrate_conf_d_refuses_a_destination_outside_the_tree(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: ..\n"})
    r = _mcd(root)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "refusing to move" in r.stderr and "not a path inside" in r.stderr, r.stderr
    assert "da-guard parse-tree" not in r.stderr, r.stderr


def test_threshold_govern_a_failing_da_guard_is_that_tenants_error(tmp_path, monkeypatch):
    import _lib_parse_tree as pt
    import threshold_govern as tg

    def boom(*a, **k):
        raise pt.ParseTreeError("da-guard parse-tree did not finish within 120s")
    monkeypatch.setattr(tg, "verify_only_changed", boom)
    monkeypatch.setattr(tg, "http_get_json", lambda url, timeout, headers: (
        {"raw_yaml": 'tenants:\n  tenant-a:\n    k1: "10"\n'}, None))
    plan = tg.TenantPlan("tenant-a", [tg.PlannedChange("k1", "10", 15.0, '"15"', 50.0,
                                                       "HIGH", 15.0, "")])
    args = type("A", (), {"tenant_api_url": "http://x", "timeout": 1, "config_dir": str(tmp_path),
                          "identity_email": "", "identity_groups": "", "auth_token": "",
                          "auth_token_file": ""})()
    out = tg.open_governance_pr(plan, args)
    assert out.status == "error" and "verify could not run" in out.message, out
    monkeypatch.setattr(tg, "verify_only_changed",
                        lambda *a, **k: (_ for _ in ()).throw(pt.BinaryMissing("no da-guard")))
    with pytest.raises(pt.BinaryMissing):
        tg.open_governance_pr(plan, args)


def test_patch_config_refuses_keys_that_differ_only_in_case():
    import patch_config as pc
    cm = {"data": {"_defaults.yaml": _PC_DEFAULTS,
                   "Tenant-A.yaml": "tenants:\n  other: {}\n",
                   "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n"}}
    with pytest.raises(pc.ConfigMapShapeError, match="differ only in case"):
        pc.build_patch(cm, "multi-file", "tenant-a", "cpu", "6")


# ── onboard_platform（#2906 F1：Phase 1 的 <tenant>.yaml、Phase 2 的 _defaults 建議）──

_OB_AM = ("route:\n  receiver: d\n  routes:\n"
          "  - matchers: ['tenant=\"tenant-a\"']\n    receiver: a\n    group_wait: {GW}\n"
          "receivers:\n- name: d\n- name: a\n  webhook_configs:\n"
          "  - url: https://hook.example/a\n")


def _onb(tmp_path, group_wait, env=None):
    am = tmp_path / "am.yml"
    am.write_text(_OB_AM.replace("{GW}", group_wait), encoding="utf-8")
    out = tmp_path / "out"
    r = _run("ops/onboard_platform.py", "--alertmanager-config", str(am),
             "-o", str(out), env=env)
    return r, out


def test_onboard_platform_writes_what_reads_back(tmp_path):
    r, out = _onb(tmp_path, "30s")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "group_wait: 30s" in (out / "phase1-routing" / "tenant-a.yaml").read_text(
        encoding="utf-8")


def test_onboard_platform_refuses_a_generated_text_that_reads_back_differently(tmp_path):
    # The string '1e3' is dumped plain by PyYAML (not a float in YAML 1.1) and
    # read by yaml.v3 as !!float: no file of the run is written, not even the
    # CSV or the hints.
    r, out = _onb(tmp_path, "'1e3'")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "phase1-routing/tenant-a.yaml" in r.stderr and "group_wait" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}


def test_onboard_platform_does_not_write_a_path_no_reader_reads(tmp_path):
    # The CLI refuses an invalid tenant id first (ADR-035); a library caller
    # reaches the confirmation, which refuses the hidden path.
    import onboard_platform as op
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        op.write_outputs(str(out), phase1_results=(
            {".hidden": {"receiver": {"type": "webhook", "url": "https://h.example/x"}}},
            {"dedup_tenants": {}}))
    assert exc.value.code == 2
    assert not out.exists() or _bytes(out) == {}


def test_onboard_platform_without_da_guard_is_exit_2(tmp_path):
    r, out = _onb(tmp_path, "30s", env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "da-guard" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}


def test_onboard_platform_defaults_suggestion_is_numbers_the_exporter_reads(tmp_path):
    # The suggestion is "merge into conf.d/_defaults.yaml"; the exporter decodes
    # `defaults:` into float64, so a quoted '80' dropped the whole block. Put
    # the generated file into a tree as `_defaults.yaml`: da-guard serves it.
    rules = tmp_path / "rules.yml"
    rules.write_text("groups:\n- name: g\n  rules:\n"
                     "  - alert: Deep\n    expr: app_queue_depth > 1e3\n"
                     "    labels: {severity: warning}\n"
                     "  - alert: Ratio\n    expr: app_err_ratio > 0.5\n"
                     "    labels: {severity: warning}\n", encoding="utf-8")
    out = tmp_path / "out"
    r = _run("ops/onboard_platform.py", "--rule-files", str(rules), "-o", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    tree = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    (tree / "_defaults.yaml").write_bytes(
        (out / "phase2-rules" / "_defaults-suggestion.yaml").read_bytes())
    sv = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                         str(tree)], capture_output=True, text=True, encoding="utf-8",
                        timeout=120)
    doc = json.loads(sv.stdout)
    assert doc["parse_failed"] == [], sv.stderr
    values = doc["tenants"]["tenant-a"]["values"]
    assert (values["app_queue_depth"], values["app_err_ratio"]) == (1000, 0.5), values


def test_onboard_platform_leaves_a_non_number_out_of_defaults(tmp_path):
    import onboard_platform as op
    [(rel, text, data)] = op._phase2_confd(([
        {"status": "perfect", "metric_key": "a_b", "severity": "warning",
         "threshold_value": "1.2.3"},
        {"status": "perfect", "metric_key": "c_d", "severity": "warning",
         "threshold_value": "70"}], [], {}))
    assert "a_b: threshold '1.2.3' is not a number" in text, text
    assert list(data["defaults"]) == ["c_d"], data


# ── migrate_rule（#2906 F1：tenant-config.yaml、defaults-snippet.yaml）──────

def _mr(tmp_path, threshold, env=None):
    rules = tmp_path / "rules.yml"
    rules.write_text("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
                     f"    expr: app_queue_depth > {threshold}\n"
                     "    labels: {severity: warning}\n", encoding="utf-8")
    out = tmp_path / "out"
    return _run("ops/migrate_rule.py", str(rules), "-o", str(out), env=env), out


def test_migrate_rule_writes_what_reads_back(tmp_path):
    r, out = _mr(tmp_path, "1e3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'custom_app_queue_depth: "1e3"' in (out / "tenant-config.yaml").read_text(
        encoding="utf-8")
    assert "custom_app_queue_depth: 1000" in (out / "defaults-snippet.yaml").read_text(
        encoding="utf-8")


def test_migrate_rule_refuses_a_generated_text_that_reads_back_differently(tmp_path):
    # tenant-config.yaml writes each key as it is: a prefix holding ` #`
    # turns the line into a comment, the file into a bare scalar. No file of
    # the run is written (rule packs and report included).
    rules = tmp_path / "rules.yml"
    rules.write_text("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
                     "    expr: app_queue_depth > 80\n", encoding="utf-8")
    out = tmp_path / "out"
    r = _run("ops/migrate_rule.py", str(rules), "--prefix", "team #", "-o", str(out))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "tenant-config.yaml" in r.stderr and "text_unreadable" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}


def test_migrate_rule_writes_a_default_past_int64(tmp_path):
    # 1e20 is the int 100000000000000000000, which yaml.v3 reads as !!float:
    # a number either way to the exporter's float64 `defaults:` — written.
    r, out = _mr(tmp_path, "1e20")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "custom_app_queue_depth: 100000000000000000000" in (
        out / "defaults-snippet.yaml").read_text(encoding="utf-8")


def test_migrate_rule_without_da_guard_is_exit_2(tmp_path):
    r, out = _mr(tmp_path, "80", env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "da-guard" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}


def test_migrate_rule_dry_run_and_triage_need_no_da_guard(tmp_path):
    # Neither writes a conf.d file, so neither asks da-guard.
    for mode in ("--dry-run", "--triage"):
        rules = tmp_path / "rules.yml"
        rules.write_text("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
                         "    expr: app_queue_depth > 80\n", encoding="utf-8")
        r = _run("ops/migrate_rule.py", str(rules), mode, "-o", str(tmp_path / mode),
                 env=_no_da_guard(tmp_path))
        assert r.returncode == 0, (mode, r.stdout + r.stderr)


# ── generate_tenant_fixture（#2906 F1：整棵 conf.d 測試樹）─────────────────

def _fx(out, *extra, env=None):
    return _run("dx/generate_tenant_fixture.py", "-n", "5", "--layout", "hierarchical",
                "--with-defaults", "-o", str(out), *extra, env=env)


def test_generate_tenant_fixture_writes_what_reads_back(tmp_path):
    out = tmp_path / "conf.d"
    r = _fx(out, "--extra-defaults", "bench_trigger=50",
            "--extra-defaults", "bench_ratio=1.5")
    assert r.returncode == 0, r.stdout + r.stderr
    tree = _lib_parse_tree.load(out, None, 120)
    assert not tree.unreadable and len(tree.selected) == len(_bytes(out)), tree.not_read
    assert tree.node("_defaults.yaml")["defaults"]["bench_ratio"]["tag"] == "!!float"


def test_generate_tenant_fixture_refuses_a_generated_text_that_reads_back_differently(
        tmp_path):
    # `inf` is a Python float written `inf`, which yaml.v3 reads as the
    # string "inf" (the exporter would drop the whole `defaults:` block).
    # All or none: the output directory holds nothing but itself — no
    # tenant file, no domain directory, no staging directory beside it.
    out = tmp_path / "conf.d"
    r = _fx(out, "--extra-defaults", "bench_trigger=inf")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "_defaults.yaml" in r.stderr and "bench_trigger" in r.stderr, r.stderr
    assert list(out.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["conf.d"]


def test_generate_tenant_fixture_is_the_same_bytes_whatever_the_hash_seed(tmp_path):
    # The domain `_defaults.yaml` took its db types from a `set`: their order
    # (and so the values the seeded RNG gave them) changed with the
    # per-process string hash.
    trees = []
    for hash_seed in ("1", "2"):
        out = tmp_path / hash_seed
        r = _fx(out, env={**os.environ, "PYTHONHASHSEED": hash_seed})
        assert r.returncode == 0, r.stdout + r.stderr
        trees.append(_bytes(out))
    assert trees[0] == trees[1]
    assert any(k.endswith("/_defaults.yaml") for k in trees[0]), sorted(trees[0])


def test_generate_tenant_fixture_without_da_guard_is_exit_2(tmp_path):
    out = tmp_path / "conf.d"
    r = _fx(out, env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "da-guard" in r.stderr, r.stderr
    assert _bytes(out) == {}


def test_generate_tenant_fixture_plain_thresholds_are_numbers(tmp_path):
    # The written `key: 123` is a yaml.v3 !!int; the data now says so too —
    # a `str` here would be a !!str expectation the text never meets.
    import generate_tenant_fixture as gtf
    import random
    rng = random.Random(7)
    vals = [gtf._gen_threshold_value(rng) for _ in range(200)]
    assert {type(v) for v in vals} == {int, str}
    assert all(not v.isdigit() for v in vals if isinstance(v, str))


# ── _lib_write_check.confirm_tree（批次：一次 tree mode）──────────────────

def test_confirm_tree_moves_only_after_every_file_reads_back(tmp_path):
    import _lib_write_check as wc
    root = tmp_path / "out"
    good = ("tenant-a.yaml", "tenants:\n  tenant-a:\n    k: '1'\n",
            wc.from_python({"tenants": {"tenant-a": {"k": "1"}}}))
    bad = ("tenant-b.yaml", "tenants:\n  tenant-b:\n    k: 1e3\n",
           wc.from_python({"tenants": {"tenant-b": {"k": "1e3"}}}))
    with pytest.raises(wc.WriteRefused) as exc:
        with wc.confirm_tree(root, [good, bad]):
            raise AssertionError("entered despite a refusal")
    assert exc.value.reason == wc.CONTENT_DIFFERS and exc.value.rel == "tenant-b.yaml"
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # no staging left
    root.mkdir()
    with wc.confirm_tree(root, [good]) as staged:
        staged.place("tenant-a.yaml", root / "tenant-a.yaml")
    assert (root / "tenant-a.yaml").read_text(encoding="utf-8") == good[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out"]


def test_confirm_tree_refuses_a_path_no_reader_reads(tmp_path):
    import _lib_write_check as wc
    with pytest.raises(wc.WriteRefused) as exc:
        with wc.confirm_tree(tmp_path / "out", [
                (".hidden.yaml", "tenants: {}\n", {"tenants": {}})]):
            pass
    assert exc.value.reason == wc.NOT_READ


def test_confirm_tree_judges_against_a_non_empty_root_in_file_mode(tmp_path):
    # With files already in root the staged tree is not the tree the run
    # leaves: beside an existing `_defaults.yaml`, a new `_defaults.yml` is a
    # spelling the directory does not select (alone in the staged tree it
    # would be) — refused as file mode does.
    import _lib_write_check as wc
    root = _tree(tmp_path / "out", {"_defaults.yaml": "defaults:\n  k: 1\n"})
    with pytest.raises(wc.WriteRefused) as exc:
        with wc.confirm_tree(root, [("_defaults.yml", "defaults:\n  k: 2\n",
                                     wc.from_python({"defaults": {"k": 2}}))]):
            pass
    assert exc.value.reason == wc.NOT_READ, exc.value


def test_confirm_tree_without_da_guard_raises(tmp_path, monkeypatch):
    import _lib_write_check as wc
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    with pytest.raises(_lib_parse_tree.ParseTreeError):
        with wc.confirm_tree(tmp_path / "out", [
                ("t.yaml", "tenants: {}\n", {"tenants": {}})]):
            pass
    assert sorted(p.name for p in tmp_path.iterdir()) == []
