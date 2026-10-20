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
`<tenant>.yaml`（init 的既有檔規則見 tests/ops/test_init_project_existing_carriers.py）；
check_threshold_registry `--regen` 的第 4 列——它只寫兩個固定路徑、各在自己 conf.d 根目錄的
`_defaults.yaml`，一定有讀取端。

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


# ── check_threshold_registry --regen (two conf.d `_defaults.yaml`) ───────
# `--regen` splices generated blocks into the dev template and the try-local
# seed. The rows run `regen_surfaces` on copies of the two real files in a
# tmp repo root, with only the conf.d surfaces (the pack headers and helm
# values are not conf.d, and would be the real files). The two files sit at
# their conf.d roots under fixed names, so there is no row 4.

_DEV_REL = "components/threshold-exporter/config/conf.d/_defaults.yaml"
_SEED_REL = "try-local/seed/conf.d/_defaults.yaml"


def _regen_setup(tmp_path, monkeypatch, dev_text=None, seed_text=None):
    """(lib, dev path, seed path): copies of the two files (made stale, so a
    regen has something to write) and `regen_surfaces` aimed at them."""
    import _registry_lib as lib
    root = tmp_path / "repo"
    dev, seed = root / "dev" / "conf.d" / "_defaults.yaml", root / "seed" / "conf.d" / "_defaults.yaml"
    for p, rel, text in ((dev, _DEV_REL, dev_text), (seed, _SEED_REL, seed_text)):
        p.parent.mkdir(parents=True)
        p.write_text(text if text is not None else
                     (REPO / rel).read_text(encoding="utf-8"), encoding="utf-8")
    # The registry too, made stale (a comment line): a `--regen` that wrote it
    # would change its bytes.
    registry = root / "rule-packs" / "threshold-registry.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text((REPO / "rule-packs" / "threshold-registry.yaml").read_text(
        encoding="utf-8") + "# stale\n", encoding="utf-8")
    monkeypatch.setattr(lib, "REGISTRY_PATH", str(registry))
    monkeypatch.setattr(lib, "_REPO_ROOT", str(root))
    monkeypatch.setattr(lib, "DEV_DEFAULTS_PATH", str(dev))
    monkeypatch.setattr(lib, "TRY_LOCAL_DEFAULTS_PATH", str(seed))
    real = lib.surface_specs
    monkeypatch.setattr(lib, "surface_specs",
                        lambda doc: [s for s in real(doc) if s.get("confd")])
    return lib, dev, seed


def _stale(rel, old, new):
    text = (REPO / rel).read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    return text.replace(old, new)


_DEV_STALE = ("  container_cpu: 80\n", "  container_cpu: 81\n  gone_key: 1\n")
_SEED_STALE = ("  - oracle_wait_time_rate   # s/s — Wait time rate (5m)\n",
               "  - oracle_wait_time_rate   # s/s — Wait time rate (5m)\n  - gone_key\n")


def test_threshold_registry_regen_writes_what_reads_back(tmp_path, monkeypatch):
    """A stale value and a key the registry no longer has, in both files:
    the regen writes them back to the committed bytes."""
    lib, dev, seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE),
                                  _stale(_SEED_REL, *_SEED_STALE))
    touched = lib.regen_surfaces(lib.build_registry_doc())
    assert sorted(touched) == sorted([str(dev), str(seed)])
    assert dev.read_bytes() == (REPO / _DEV_REL).read_bytes()
    assert seed.read_bytes() == (REPO / _SEED_REL).read_bytes()


def test_threshold_registry_regen_refuses_a_text_that_reads_back_differently(
        tmp_path, monkeypatch):
    # A registry value of +inf renders `inf`, a string to yaml.v3 — not the
    # number the exporter's `defaults` takes. Neither file is written (the
    # seed's change alone would read back fine: all or none).
    lib, dev, seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE),
                                  _stale(_SEED_REL, *_SEED_STALE))
    before = _bytes(tmp_path / "repo")
    doc = lib.build_registry_doc()
    doc["keys"]["container_cpu"]["value"] = float("inf")
    with pytest.raises(lib.ConfdWriteRefused) as exc:
        lib.regen_surfaces(doc)
    assert "content_differs" in str(exc.value) and "defaults.container_cpu" in str(exc.value)
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_does_not_write_an_unreadable_original(tmp_path, monkeypatch):
    # A key written twice outside the generated block: PyYAML kept the last
    # one and the regen wrote; yaml.v3 refuses the file.
    text = _stale(_DEV_REL, *_DEV_STALE) + "flag: 1\nflag: 2\n"
    lib, dev, _seed = _regen_setup(tmp_path, monkeypatch, text)
    before = _bytes(tmp_path / "repo")
    with pytest.raises(lib.ConfdWriteRefused, match="original_unreadable.*duplicate_key"):
        lib.regen_surfaces(lib.build_registry_doc())
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_without_da_guard_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(TOOLS / "lint"))
    import check_threshold_registry as gate
    lib, _dev, _seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE))
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    before = _bytes(tmp_path / "repo")
    assert gate.main(["--regen"]) == 2
    assert "da-guard parse-tree" in capsys.readouterr().err
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_judges_the_value_by_yaml_v3(tmp_path, monkeypatch):
    # 1e20 renders `1e+20`: a float to yaml.v3 (the exporter reads it as a
    # number), a string to PyYAML's YAML 1.1 — written.
    lib, dev, _seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE))
    doc = lib.build_registry_doc()
    doc["keys"]["container_cpu"]["value"] = 1e20
    lib.regen_surfaces(doc)
    assert "  container_cpu: 1e+20\n" in dev.read_text(encoding="utf-8")


def test_threshold_registry_regen_refused_writes_not_even_the_registry(
        tmp_path, monkeypatch, capsys):
    """A refused conf.d surface: rc 2 and every file — the registry and both
    `_defaults.yaml` — keeps its bytes (the registry used to be written
    before the surfaces were judged)."""
    monkeypatch.syspath_prepend(str(TOOLS / "lint"))
    import check_threshold_registry as gate
    _lib, _dev, _seed = _regen_setup(
        tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE) + "flag: 1\nflag: 2\n",
        _stale(_SEED_REL, *_SEED_STALE))
    before = _bytes(tmp_path / "repo")
    assert gate.main(["--regen"]) == 2
    assert "original_unreadable" in capsys.readouterr().err
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_writes_the_registry_and_surfaces(tmp_path, monkeypatch):
    """The same run without the refusal writes all three back to the
    committed bytes — so the row above is not passing on a run that writes
    nothing anyway."""
    monkeypatch.syspath_prepend(str(TOOLS / "lint"))
    import check_threshold_registry as gate
    _lib, dev, seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE),
                                   _stale(_SEED_REL, *_SEED_STALE))
    assert gate.main(["--regen"]) == 0
    registry = tmp_path / "repo" / "rule-packs" / "threshold-registry.yaml"
    assert registry.read_bytes() == (REPO / "rule-packs" / "threshold-registry.yaml").read_bytes()
    assert dev.read_bytes() == (REPO / _DEV_REL).read_bytes()
    assert seed.read_bytes() == (REPO / _SEED_REL).read_bytes()
