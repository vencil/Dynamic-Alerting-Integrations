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

deprecate_rule、patch_config、threshold_govern 改既有檔時改由 `da-guard write` 寫
（ADR-036 步驟 10b，#3206）：第 2 列對它們是「edit 拒寫就不寫」（PyYAML 換寫法的那種
讀回不同已不會發生，改釘「沒動到的寫法照原樣」），deprecate_rule 另有一列「多檔中一個
被拒就都不寫」。

各工具既有測試檔裡同性質的列（例如 patch_config 的 TestDecisionsAreParseTrees、
deprecate_rule 的封閉清單表）照留，這裡是逐工具、同一張表的那一份。

沒有的列：patch_config 的第 4 列——它只寫 ConfigMap 裡宣告租戶的 key 或新建
`<tenant>.yaml`，名字先經 `is_carrier_candidate`（YAML 副檔名、非隱藏、非 `_`），平面
目錄裡這樣的名字一定有讀取端；da_assembler 的第 3、4 列——它從 CR 整份重新產生檔案、
不以舊檔為基礎（舊檔不讀，`replace` 整份取代），檔名是 DNS-1123 名稱加 `.yaml`（兩條
路徑都先驗），不會是隱藏或 `_` 名；
init_project／scaffold_tenant 的第 3、4 列——只寫新產生的 `_defaults.yaml` 與
`<tenant>.yaml`（init 的既有檔規則見 tests/ops/test_init_project_existing_carriers.py）；
check_threshold_registry `--regen` 的第 4 列——它只寫兩個固定路徑、各在自己 conf.d 根目錄的
`_defaults.yaml`，一定有讀取端。
onboard_platform／migrate_rule／generate_tenant_fixture（#2906 F1）同屬「產生新檔」：
沒有第 3 列（不讀原檔）；第 4 列只有 onboard_platform 有——它的檔名是租戶 id，
CLI 先以 ADR-035 擋下不合法的 id，函式層呼叫則由確認擋下；migrate_rule 的兩個檔名
固定，generate_tenant_fixture 的檔名是 `<domain>-<db>-<NNNN>.yaml` 與
`_defaults.yaml`。第 6 列對產生新檔的工具就是第 2 列：預期內容是工具要寫的資料，
判定是 yaml.v3 讀回的型別。generate_tenant_fixture、onboard_platform、migrate_rule、
scaffold_tenant、init_project 改由 `da-guard write` 一次一批寫（#3206 步驟 10c）：文字是
Go 的，第 2 列成為「值沒有契約型別（`inf`）就不寫」與「write 拒寫一個檔就一個都不寫」；
PyYAML 換寫法的那種讀回不同已不會發生（`'1e3'`、含 `#` 的鍵改釘「寫成雙引號、讀回原
值」）。onboard_platform、migrate_rule、scaffold_tenant、init_project 重跑同一個 -o
照舊覆寫（`replace`），拒寫一律結束碼 2。
migrate_conf_d 搬檔（#3206 步驟 10）：整個搬移計畫是一批 `da-guard write` 的搬移
項目，`--dry-run` 也問同一批（`--dry-run`）。第 1 列是「搬得過去就搬、位元組不變」，
第 2 列是「一個被拒就一個都不搬」（目的地沒有讀取端、在 conf.d 外），第 3 列是讀不了的
原檔，第 4 列是隱藏的目的地；它的拒絕一律結束碼 2。
da_assembler 同樣改由 `da-guard write` 寫（#3206 步驟 10c；CR 刪除時的刪檔也是）：第 2 列
成為「write 拒寫就不寫」與「`defaults` 的字串一律拒」，第 5 列 `--render-cr` 是 exit 2
（controller 是例外、不結束）。
offboard_tenant 的刪檔同樣改由 `da-guard write` 的刪除項目做（#3206 步驟 10）：第 2 列成為
「write 拒刪就不刪」（symlink；拿掉後樹變壞的 `worsens_tree`），結束碼 2；它不寫內容，
所以沒有「讀回不同」那一種。
assemble_config_dir 組裝（#3206 步驟 10）：多個來源 conf.d 的檔是一批 `da-guard write`
的 `copy` 項目（`replace`：`--output` 裡同名的檔整份取代），`--check` 問同一批的 dry
run。第 1 列是「位元組原樣放進去」，第 2 列是「一個被拒就一個都不寫」（`--output` 裡
同名的是 symlink），第 3 列是讀不了的來源檔（共用的 fail-closed 在送批次前就擋下）；
沒有第 4 列——檔名取自來源自己的 walk、放進 `--output` 的根目錄，有讀取端；`_defaults`
的另一種拼法撞上 `--output` 裡已有的那個是工具自己的衝突檢查（rc 1，
tests/ops/test_assemble_config_dir.py）。沒有第 6 列——它不從值做任何決定，只搬位元組
（跨來源重複租戶是 `_lib_tenant_uniqueness` 的、在它自己的測試裡）。拒寫一律結束碼 2。
check_threshold_registry `--regen` 同樣改由 `da-guard write` 寫（#3206 步驟 10）：拼好生成
區塊的兩個 `_defaults.yaml` 各是一個 `replace`（整份新文字），每個 conf.d 根目錄一批，
寫之前先問同一批的 dry run。第 2 列是「write 拒寫就不寫」（`inf` 驗證會點名），另有
「兩個根目錄的拒絕逐檔點名」、「權限保留」與「symlink 拒寫」；第 3 列成為新文字讀不了
（原檔的重複鍵留在拼好的文字裡：`read_back_mismatch`）。

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


def test_deprecate_rule_keeps_an_untouched_spelling_as_written(tmp_path):
    # #3206: the text is da-guard write's, so `!!float 1` stays `!!float 1`
    # (PyYAML dumped `1.0` and the parse-tree confirmation refused the run).
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": "defaults:\n  cpu_usage: 80\n  mem_usage: !!float 1\n",
        "tenant-a.yaml": _DR_TENANT})
    r = _dr(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / "_defaults.yaml").read_text(encoding="utf-8") == (
        "defaults:\n  mem_usage: !!float 1\n")


def test_deprecate_rule_writes_nothing_when_one_file_is_refused(tmp_path):
    # Two-phase (`_lib_confedit.edit_all`): da-guard write refuses one file
    # (a symlinked tenant file: edit never rewrites a link), so neither it nor
    # the carrier and the other tenant file — each fine on its own — is
    # written; every refusal is named, rc 1.
    from _platform_fs import symlink_or_skip
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": _DR_DEFAULTS, "tenant-a.yaml": _DR_TENANT,
        "b.txt": "tenants:\n  tenant-b:\n    cpu_usage: '70'\n"})
    symlink_or_skip("b.txt", root / "tenant-b.yaml")
    before = _bytes(root)
    r = _dr(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "本輪降級為預覽" in r.stdout, r.stdout
    tail = r.stdout.split("下架未完成", 1)[1]
    assert "tenant-b.yaml" in tail and "symlink" in tail, tail
    assert _bytes(root) == before


def test_deprecate_rule_does_not_write_an_unreadable_file(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "_defaults.yaml": _DR_DEFAULTS,
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu_usage: '85'\n    cpu_usage: '86'\n"})
    before = (root / "tenant-a.yaml").read_bytes()
    r = _dr(root)
    # #3206: a file the exporter cannot decode refuses the run before any write.
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ERROR: tenant-a.yaml:" in r.stderr and "duplicate_key" in r.stderr, r.stderr
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
        finally:
            # apply leaves SIGINT/SIGTERM at SIG_IGN to the end of the process
            # (the CLI's contract); in-process that is this xdist worker, and
            # every later test's children would start with SIGTERM ignored
            # (#3249). The seam puts the caller's handlers back.
            pc.SIGNALS.reset()
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
    # #3206: the text is da-guard write's, so an untouched `!!float 1` is kept
    # as written (PyYAML wrote `1.0` and the confirmation refused it). What is
    # refused now is an edit da-guard cannot make without changing another
    # node: `mem: *v` aliases the scalar being replaced.
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: &v '5'\n    mem: *v\n"},
                     "tenant-a", "cpu", "6")
    assert (rc, doc["status"], doc["reason"]) == (2, "caller_error", "write_refused"), doc
    assert c.patches == []
    rc, doc, c = _pc({"_defaults.yaml": _PC_DEFAULTS,
                      "tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: '5'\n    mem: !!float 1\n"},
                     "tenant-a", "cpu", "6")
    assert rc == 0, doc
    assert "mem: !!float 1\n" in c.data["tenant-a.yaml"], c.data


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
    assert patch == {"data": {"team.yaml": "tenants:\n  on:\n    cpu: 6\n"}}, patch


# ── da_assembler（CR 渲染出的 <name>.yaml；#3206 10c 起由 da-guard write 寫、刪）──

def _cr(tenants, defaults=None, name="ok"):
    spec = {"tenants": tenants}
    if defaults is not None:
        spec["defaults"] = defaults
    return {"kind": "ThresholdConfig", "metadata": {"name": name, "namespace": "ns"},
            "spec": spec}


def test_da_assembler_writes_what_reads_back(tmp_path):
    import da_assembler
    da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}, {"cpu": 1000}),
                               tmp_path, cli=True)
    text = (tmp_path / "ok.yaml").read_text(encoding="utf-8")
    assert text.startswith("# Auto-generated by da-assembler from ThresholdConfig ns/ok\n"), text
    assert 'cpu: "70"' in text and "cpu: 1000\n" in text, text


def test_da_assembler_writes_a_number_like_string_as_a_string(tmp_path):
    # #3206 10c: the text is Go's. The tenant value "1e3" (a JSON string,
    # once dumped plain by PyYAML, read by yaml.v3 as !!float, refused) is
    # written double-quoted: it reads back as the string it is.
    import da_assembler
    da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "1e3"}}), tmp_path, cli=True)
    assert 'cpu: "1e3"' in (tmp_path / "ok.yaml").read_text(encoding="utf-8")


def test_da_assembler_rerun_replaces_its_own_file(tmp_path):
    # The file is generated whole from the CR: a rerun replaces it whole
    # (`replace`), the header written once.
    import da_assembler
    da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}), tmp_path, cli=True)
    da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "80"}}), tmp_path, cli=True)
    text = (tmp_path / "ok.yaml").read_text(encoding="utf-8")
    assert 'cpu: "80"' in text and '"70"' not in text, text
    assert text.count("Auto-generated") == 1, text


def test_da_assembler_does_not_write_a_refused_file(tmp_path, capsys):
    # da-guard write refuses the destination (a symlink): rc 2 naming it,
    # nothing written. The controller takes the same refusal as an
    # exception (tests/ops/test_da_assembler.py), never an exit.
    import da_assembler
    from _platform_fs import symlink_or_skip
    (tmp_path / "elsewhere.yaml").write_text("tenants: {}\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    symlink_or_skip(str(tmp_path / "elsewhere.yaml"), out / "ok.yaml")
    before = _bytes(out)
    with pytest.raises(SystemExit) as exc:
        da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}), out, cli=True)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "ok.yaml: symlink" in err and "nothing was written" in err, err
    assert _bytes(out) == before and (out / "ok.yaml").is_symlink()


def test_da_assembler_without_da_guard_is_exit_2(tmp_path, monkeypatch, capsys):
    import da_assembler
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        da_assembler.reconcile_one(_cr({"tenant-a": {"cpu": "70"}}), out, cli=True)
    assert exc.value.code == 2
    assert "da-guard" in capsys.readouterr().err
    assert not out.exists()  # --config-dir is not even created


def test_da_assembler_defaults_are_numbers_the_exporter_reads(tmp_path):
    # The exporter decodes `defaults:` into float64. A number is written as
    # one (an int past 18 digits as the float it is); a JSON string is
    # refused whatever its text — Go writes it as a string, which the
    # exporter cannot decode (#3206: "1e3" and "0o17" alike, once written
    # plain and read as numbers).
    import da_assembler
    da_assembler.reconcile_one(_cr({"tenant-a": {}}, {"cpu": 10**20, "mem": 0.5}),
                               tmp_path, cli=True)
    text = (tmp_path / "ok.yaml").read_text(encoding="utf-8")
    assert "cpu: 100000000000000000000.0\n" in text and "mem: 0.5\n" in text, text
    sv = subprocess.run([os.environ["DA_GUARD_BINARY"], "served-values", "--config-dir",
                         str(tmp_path)], capture_output=True, text=True, encoding="utf-8",
                        timeout=120, check=False)
    assert sv.returncode == 0, sv.stdout + sv.stderr  # the exporter reads the file
    assert json.loads(sv.stdout)["parse_failed"] == [], sv.stderr
    for text in ("1e3", "0o17", "80"):
        with pytest.raises(ValueError, match="must be a number, not the string"):
            da_assembler.reconcile_one(_cr({"tenant-a": {}}, {"cpu": text}, name="ok2"),
                                       tmp_path, cli=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ok.yaml"]


def test_da_assembler_refuses_a_tenant_id_and_a_name_before_any_write(tmp_path):
    # ADR-035's tenant id rule (`is_valid_tenant_id`) and the Kubernetes
    # object name rule (DNS-1123 subdomain) for the file name: refused
    # before da-guard is asked, nothing written.
    import da_assembler
    with pytest.raises(ValueError, match="not a valid tenant id"):
        da_assembler.reconcile_one(_cr({"Tenant_A": {}}), tmp_path, cli=True)
    with pytest.raises(ValueError, match="not a valid Kubernetes object name"):
        da_assembler.reconcile_one(_cr({"tenant-a": {}}, name="_defaults"), tmp_path,
                                   cli=True)
    assert list(tmp_path.iterdir()) == []


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


def test_init_project_refuses_a_value_with_no_contract_type(tmp_path, monkeypatch):
    # #3206 10c-3: the text is Go's, so "reads back differently" cannot
    # happen; a value the edit contract has no type for (inf) is not sent.
    import init_project as ip
    real = ip._tenant_override_rows
    monkeypatch.setattr(ip, "_tenant_override_rows",
                        lambda t, packs: {**real(t, packs), "probe_key": float("inf")})
    out = tmp_path / "repo"
    with pytest.raises(ip._ConfdWriteRefused) as exc:
        ip.run_init(_init_config(["tenant-a"]), str(out))
    assert "probe_key" in str(exc.value) and "tenant-a.yaml" in str(exc.value)
    assert not out.exists() or _bytes(out) == {}


def test_init_project_one_refused_file_writes_none(tmp_path, monkeypatch, capsys):
    # One batch: tenant-b's file (made to declare tenant-a too) worsens the
    # tree — a tenant declared twice — so tenant-a's file and `_defaults.yaml`
    # are not written either, nor anything else of the run.
    import init_project as ip
    real = ip._tenant_file

    def twice(tenant, packs):
        f = real(tenant, packs)
        if tenant == "tenant-b":
            f.data["tenants"]["tenant-a"] = {}
        return f
    monkeypatch.setattr(ip, "_tenant_file", twice)
    out = tmp_path / "repo"
    with pytest.raises(SystemExit) as exc:
        ip.run_init(_init_config(["tenant-a", "tenant-b"]), str(out))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "init_project: ERROR:" in err and "nothing was written" in err, err
    assert "duplicate_tenant" in err, err
    # Nothing of the run, and no empty conf.d/ or -o directory either.
    assert not out.exists(), sorted(p.as_posix() for p in out.rglob("*"))


@pytest.mark.parametrize("pack, why", [("mariadb\rx", "U+000D"),
                                       ("redis\nevil: 1", "line break")])
def test_init_project_refuses_a_rule_pack_no_comment_can_carry(tmp_path, pack, why):
    # The pack names go into one line of `_defaults.yaml`'s header comment:
    # refused before the batch, naming the input — not the whole batch's
    # bad_input, nor the header's broken line.
    import init_project as ip
    out = tmp_path / "repo"
    with pytest.raises(ip._ConfdWriteRefused) as exc:
        ip.run_init({**_init_config(["tenant-a"]), "rule_packs": [pack]},
                    str(out))
    assert "--rule-packs" in str(exc.value) and why in str(exc.value)
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


# ── scaffold_tenant ───────────────────────────────────────────────────────

def test_scaffold_tenant_writes_what_reads_back(tmp_path):
    r = _run("ops/scaffold_tenant.py", "--tenant", "tenant-a", "--db", "mariadb",
             "-o", str(tmp_path / "out"), "--non-interactive")
    assert r.returncode == 0, r.stdout[-400:] + r.stderr[-800:]
    assert (tmp_path / "out" / "tenant-a.yaml").is_file()


def test_scaffold_tenant_refuses_a_value_with_no_contract_type(tmp_path, capsys):
    # #3206 10c-3: a value the edit contract has no type for (inf) is not
    # sent — rc 2, named, and nothing of the run written (the report neither).
    import scaffold_tenant as st
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        st.write_outputs(str(out), "tenant-a", {"defaults": {"cpu": 70}},
                         {"tenants": {"tenant-a": {"cpu": float("inf")}}}, "report")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "tenant-a.yaml" in err and "tenants.tenant-a.cpu" in err, err
    assert not out.exists()


def test_scaffold_tenant_a_refused_batch_leaves_no_new_output_directory(tmp_path, capsys):
    # #3206: -o is made only when the batch is sent (`write_all_or_exit`'s
    # make_root) and removed again when da-guard refuses it — here a file
    # no reader reads.
    import _lib_confedit
    import scaffold_tenant as st
    out = tmp_path / "new" / "out"
    with pytest.raises(SystemExit) as exc:
        st._write_confd_files(out, [
            _lib_confedit.NewFile("tenant-a.yaml", {"tenants": {"tenant-a": {}}}),
            _lib_confedit.NewFile(".hidden.yaml", {"tenants": {}})])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "scaffold_tenant: ERROR: .hidden.yaml: not_read:" in err, err
    assert not (tmp_path / "new").exists()


def test_scaffold_tenant_one_refused_file_writes_none(tmp_path, capsys):
    # One batch: the `_defaults.yaml` scaffold adds deselects the
    # `_defaults.yml` already in -o (the tree gets worse) — refused, so the
    # tenant file and the report are not written either; rc 2.
    import scaffold_tenant as st
    out = _tree(tmp_path / "out", {"_defaults.yml": "defaults:\n  k: 1\n"})
    before = _bytes(out)
    with pytest.raises(SystemExit) as exc:
        st.write_outputs(str(out), "tenant-a", {"defaults": {"cpu": 70}},
                         {"tenants": {"tenant-a": {"cpu": "70"}}}, "report")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "_defaults.yml" in err and "nothing was written" in err, err
    assert _bytes(out) == before


def test_scaffold_tenant_without_da_guard_is_exit_2(tmp_path, monkeypatch):
    import scaffold_tenant as st
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        st.write_outputs(str(out), "tenant-a", {"defaults": {"cpu": 70}},
                         {"tenants": {"tenant-a": {"cpu": "70"}}}, "report")
    assert exc.value.code == 2
    # #3206: not even the -o directory is left behind.
    assert not out.exists()


# ── threshold_govern (the pre-PUT edit) ──────────────────────────────────
# #3206: the PUT body is `da-guard write`'s text, so row 2's "a text that
# reads back differently" is da-guard's own check (pkg/confedit); here: an
# untouched spelling yaml.v3 and PyYAML read differently is kept as written,
# and the value is written with the type the plan means (a string).

def test_threshold_govern_passes_an_edit_that_reads_back(tmp_path):
    import threshold_govern as tg
    old = 'tenants:\n  tenant-a:\n    k1: "10"\n    k2: 0o17\n'
    new, err = tg.edit_live_yaml(old, "tenant-a", {"k1": "15"}, config_dir=str(tmp_path))
    assert (new, err) == ('tenants:\n  tenant-a:\n    k1: "15"\n    k2: 0o17\n', None)


def test_threshold_govern_writes_the_value_as_the_string_the_plan_means(tmp_path):
    # `k1: 10` is an int; the plan writes the string "15", never the int 15.
    import threshold_govern as tg
    old = 'tenants:\n  tenant-a:\n    k1: 10\n'
    new, err = tg.edit_live_yaml(old, "tenant-a", {"k1": "15"}, config_dir=str(tmp_path))
    assert err is None and 'k1: "15"\n' in new, (new, err)


def test_threshold_govern_refuses_an_unreadable_original_and_an_unread_path(tmp_path):
    import threshold_govern as tg
    dup = 'tenants:\n  tenant-a:\n    k1: "10"\n    k1: "11"\n'
    new, err = tg.edit_live_yaml(dup, "tenant-a", {"k1": "15"}, config_dir=str(tmp_path))
    assert new is None and err and "original_unreadable" in err, err
    ok = 'tenants:\n  tenant-a:\n    k1: "10"\n'
    new, err = tg.edit_live_yaml(ok, "tenant-a", {"k1": "15"},
                                 config_dir=str(tmp_path), rel=".tenant-a.yaml")
    assert new is None and err and "(not_read)" in err, err


def test_threshold_govern_without_da_guard_raises(tmp_path, monkeypatch):
    import threshold_govern as tg
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    ok = 'tenants:\n  tenant-a:\n    k1: "10"\n'
    with pytest.raises(_lib_parse_tree.ParseTreeError):
        tg.edit_live_yaml(ok, "tenant-a", {"k1": "10"}, config_dir=str(tmp_path))


# ── migrate_conf_d ────────────────────────────────────────────────────────

def _mcd(root, *extra, env=None):
    return _run("dx/migrate_conf_d.py", "--conf-d", str(root), "--dry-run", *extra, env=env)


def test_migrate_conf_d_plans_a_move_that_reads_back(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: finance\n"})
    r = _mcd(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "tenant-a.yaml -> finance/tenant-a.yaml" in r.stdout


def test_migrate_conf_d_moves_through_da_guard_write(tmp_path, monkeypatch):
    # The moves are one `da-guard write` batch of move entries; the file
    # arrives byte for byte.
    import _lib_confedit
    import migrate_conf_d
    body = "tenants:\n  tenant-a:\n    _metadata:\n      domain: finance  # x\n"
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": body})
    sent = []
    real = _lib_confedit.write_all

    def spy(r, entries, **kw):
        sent.append((list(entries), kw.get("dry_run")))
        return real(r, entries, **kw)
    monkeypatch.setattr(_lib_confedit, "write_all", spy)
    monkeypatch.setattr(sys, "argv", ["migrate_conf_d.py", "--conf-d", str(root), "--apply"])
    migrate_conf_d.main()
    assert sent == [([{"path": "finance/tenant-a.yaml", "from": "tenant-a.yaml"}], False)]
    assert _bytes(root) == {"finance/tenant-a.yaml": body.encode("utf-8")}


def test_migrate_conf_d_refuses_a_destination_no_reader_reads(tmp_path):
    # Two moves, one to a hidden directory: the batch is refused, neither moves.
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: .hidden\n",
        "tenant-b.yaml": "tenants:\n  tenant-b:\n    _metadata:\n      domain: finance\n"})
    before = _bytes(root)
    r = _run("dx/migrate_conf_d.py", "--conf-d", str(root), "--apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "migrate_conf_d: ERROR: .hidden/tenant-a.yaml: not_read:" in r.stderr, r.stderr
    assert _bytes(root) == before
    assert not (root / "finance").exists()


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


# ── assemble_config_dir（多個來源 conf.d → 一個 config-dir；#3206 步驟 10）──

_ACD_BODY = "# team-a — 團隊\ntenants:\n  tenant-a:\n    cpu: 010 # as written\n"


def _acd(*argv, env=None):
    return _run("ops/assemble_config_dir.py", *argv, env=env)


def test_assemble_config_dir_copies_through_da_guard_write(tmp_path, monkeypatch):
    # The files are one `da-guard write` batch of copy entries; each arrives
    # byte for byte.
    import _lib_confedit
    import assemble_config_dir
    src = _tree(tmp_path / "a", {"tenant-a.yaml": _ACD_BODY})
    out = tmp_path / "out"
    sent = []
    real = _lib_confedit.write_all

    def spy(r, entries, **kw):
        sent.append((list(entries), kw.get("dry_run")))
        return real(r, entries, **kw)
    monkeypatch.setattr(_lib_confedit, "write_all", spy)
    monkeypatch.setattr(sys, "argv", ["assemble_config_dir.py", "--sources", str(src),
                                      "--output", str(out)])
    assert assemble_config_dir.main() == 0
    assert sent == [([{"path": "tenant-a.yaml", "copy": str((src / "tenant-a.yaml").resolve()),
                       "replace": True}], False)]
    assert _bytes(out) == {"tenant-a.yaml": _ACD_BODY.encode("utf-8")}


def test_assemble_config_dir_one_refused_file_writes_none(tmp_path):
    # `--output` already holds `tenant-a.yaml` as a symlink: write never
    # replaces a link, so neither it nor `tenant-b.yaml` is written.
    from _platform_fs import symlink_or_skip
    src = _tree(tmp_path / "a", {"tenant-a.yaml": _ACD_BODY,
                                 "tenant-b.yaml": "tenants:\n  tenant-b: {}\n"})
    elsewhere = _tree(tmp_path / "elsewhere", {"t.yaml": _ACD_BODY})
    out = tmp_path / "out"
    out.mkdir()
    symlink_or_skip(elsewhere / "t.yaml", out / "tenant-a.yaml")
    for mode in ((), ("--check",)):
        r = _acd("--sources", str(src), "--output", str(out), *mode)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "assemble_config_dir: ERROR: tenant-a.yaml: symlink:" in r.stderr, r.stderr
        assert sorted(p.name for p in out.iterdir()) == ["tenant-a.yaml"]


def test_assemble_config_dir_does_not_copy_an_unreadable_source(tmp_path):
    src = _tree(tmp_path / "a", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n  tenant-a: {}\n"})
    out = tmp_path / "out"
    r = _acd("--sources", str(src), "--output", str(out))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "tenant-a.yaml" in r.stderr and "duplicate_key" in r.stderr, r.stderr
    assert not out.exists()


def test_assemble_config_dir_without_da_guard_is_exit_2(tmp_path):
    src = _tree(tmp_path / "a", {"tenant-a.yaml": _ACD_BODY})
    out = tmp_path / "out"
    r = _acd("--sources", str(src), "--output", str(out), env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert not out.exists()


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
    assert r.returncode == 2, r.stdout + r.stderr  # #3206: refused before the pre-check
    assert "ERROR: other.yaml:" in r.stderr and "duplicate_key" in r.stderr, r.stderr
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
    assert r.returncode == 2, r.stdout + r.stderr  # #3206: refused before the pre-check
    assert "ERROR: tenant-a.yaml:" in r.stderr and "duplicate_key" in r.stderr, r.stderr
    assert (root / "tenant-a.yaml").exists()


def test_offboard_tenant_deletes_through_a_da_guard_write_delete_entry(tmp_path, monkeypatch):
    # #3206 步驟 10：刪檔是送給 da-guard write 的刪除項目，一批、路徑是 parse-tree
    # 的正規形式（--config-dir 怎麼拼都一樣，沒有 `./`）；Python 不自己 unlink。
    import _lib_confedit
    import offboard_tenant as ot
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "tenant-b.yaml": "tenants:\n  tenant-b: {}\n"})
    sent, real = [], _lib_confedit.write_all
    monkeypatch.setattr(_lib_confedit, "write_all",
                        lambda r, entries, **kw: sent.append(list(entries)) or real(r, entries, **kw))
    monkeypatch.setattr(os, "remove", mock.Mock(side_effect=AssertionError("os.remove")))
    monkeypatch.setattr(os, "unlink", mock.Mock(side_effect=AssertionError("os.unlink")))
    assert ot.execute_offboard("tenant-a", f"{root}/") is True
    assert sent == [[{"path": "tenant-a.yaml", "delete": True, "tenants": ["tenant-a"]}]]
    assert sorted(_bytes(root)) == ["tenant-b.yaml"]


def test_offboard_tenant_does_not_delete_a_file_da_guard_write_refuses(tmp_path):
    # The pre-check passes (the link's target declares only tenant-a), but
    # da-guard write judges the delete as a move's source: a symlink is
    # refused — rc 2 naming it, the link and its target both still there.
    from _platform_fs import symlink_or_skip
    _tree(tmp_path / "elsewhere", {"real.yaml": "tenants:\n  tenant-a: {}\n"})
    root = tmp_path / "conf.d"
    root.mkdir()
    symlink_or_skip(str(tmp_path / "elsewhere" / "real.yaml"), root / "tenant-a.yaml")
    r = _ob(root)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "offboard_tenant: ERROR: tenant-a.yaml: symlink:" in r.stderr, r.stderr
    assert "nothing was written" in r.stderr and "已刪除" not in r.stdout, r.stdout + r.stderr
    assert (root / "tenant-a.yaml").is_symlink()
    assert (tmp_path / "elsewhere" / "real.yaml").exists()


def test_offboard_tenant_precheck_asks_the_same_delete_as_a_dry_run(tmp_path):
    # #3206: without --execute the pre-check asks da-guard write the batch
    # --execute would send (--dry-run): the refused symlink is exit 2 there
    # too, named, nothing deleted.
    from _platform_fs import symlink_or_skip
    _tree(tmp_path / "elsewhere", {"real.yaml": "tenants:\n  tenant-a: {}\n"})
    root = tmp_path / "conf.d"
    root.mkdir()
    symlink_or_skip(str(tmp_path / "elsewhere" / "real.yaml"), root / "tenant-a.yaml")
    r = _run("ops/offboard_tenant.py", "tenant-a", "--config-dir", str(root))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "offboard_tenant: ERROR: tenant-a.yaml: symlink:" in r.stderr, r.stderr
    assert (root / "tenant-a.yaml").is_symlink()
    # Paired control: a regular file passes the dry run, rc 0, still there.
    plain = _tree(tmp_path / "plain", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    r = _run("ops/offboard_tenant.py", "tenant-a", "--config-dir", str(plain))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--dry-run：可刪除" in r.stdout and (plain / "tenant-a.yaml").exists(), r.stdout


def test_offboard_tenant_deletes_every_file_that_declares_the_tenant(tmp_path):
    # #3206: a tenant declared in two tenant files is served from both
    # (da-guard: duplicate tenant ID); deleting one would leave it served.
    # Both go in one batch; afterwards no file declares it.
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "tenant-a.yml": "tenants:\n  tenant-a: {cpu: '5'}\n",
                                       "other.yaml": "tenants:\n  tenant-a: {}\n",
                                       "tenant-b.yaml": "tenants:\n  tenant-b: {}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "由 3 個檔宣告" in r.stdout, r.stdout
    assert sorted(_bytes(root)) == ["tenant-b.yaml"]
    tree = _lib_parse_tree.load(root)
    assert all("tenant-a" not in (tree.node(p).get("tenants") or {}) for p in tree.selected)


def test_offboard_tenant_finds_a_tenant_declared_in_a_subdirectory(tmp_path):
    # #3206: the exporter reads the whole tree, so a tenant whose only file
    # is `team-eu/tenant-a.yaml` is served; offboard finds and deletes it.
    # The subdirectory's `_defaults.yaml` is not a tenant file: it stays.
    root = _tree(tmp_path / "conf.d", {"team-eu/tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "team-eu/_defaults.yaml": "defaults:\n  cpu: 5\n",
                                       "tenant-b.yaml": "tenants:\n  tenant-b: {}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_bytes(root)) == ["team-eu/_defaults.yaml", "tenant-b.yaml"]


def test_offboard_tenant_deletes_the_top_level_and_the_nested_copy(tmp_path):
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "team-eu/tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "tenant-b.yaml": "tenants:\n  tenant-b: {}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "由 2 個檔宣告" in r.stdout, r.stdout
    assert sorted(_bytes(root)) == ["tenant-b.yaml"]


def test_offboard_tenant_leaves_a_file_named_for_it_that_declares_nothing(tmp_path):
    # Read as a tenant file and named for tenant-a, a leftover subset record
    # `_federation/tenant-a.yaml` declares no tenant: it serves nothing of
    # tenant-a, so it is not deleted (and does not fail the pre-check).
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "_federation/tenant-a.yaml": "metrics: [cpu]\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert sorted(_bytes(root)) == ["_federation/tenant-a.yaml"]


def test_offboard_tenant_names_a_file_named_for_it_that_declares_another(tmp_path):
    # `tenant-a.yaml` is there but declares tenant-z: not "not found" — the
    # file is named, with what it declares; rc 1, nothing deleted.
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-z: {}\n"})
    before = _bytes(root)
    r = _ob(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "tenant-a.yaml 檔名是 tenant-a 但沒宣告它（宣告了 tenant-z）" in r.stdout, r.stdout
    assert "找不到設定檔案" not in r.stdout, r.stdout
    assert _bytes(root) == before


def test_offboard_tenant_names_a_file_named_for_it_beside_a_carrier(tmp_path):
    # Another file declares tenant-a: `team/tenant-a.yaml`, named for it but
    # declaring tenant-z, is still named (a warning — it is not deleted).
    root = _tree(tmp_path / "conf.d", {"tenant-a.yml": "tenants:\n  tenant-a: {}\n",
                                       "team/tenant-a.yaml": "tenants:\n  tenant-z: {}\n"})
    r = _ob(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "⚠️  " in r.stdout and "team/tenant-a.yaml 檔名是 tenant-a 但沒宣告它（宣告了 tenant-z），不刪" in r.stdout, r.stdout
    assert sorted(_bytes(root)) == ["team/tenant-a.yaml"]


def test_offboard_tenant_prints_no_pass_line_for_a_refused_dry_run(tmp_path):
    # #3206: the dry run is part of the pre-check's verdict — the refused
    # symlink never reads "✅ Pre-check 通過", with or without --execute.
    from _platform_fs import symlink_or_skip
    _tree(tmp_path / "elsewhere", {"real.yaml": "tenants:\n  tenant-a: {}\n"})
    root = tmp_path / "conf.d"
    root.mkdir()
    symlink_or_skip(str(tmp_path / "elsewhere" / "real.yaml"), root / "tenant-a.yaml")
    for extra in ((), ("--execute", "--skip-state-check")):
        r = _run("ops/offboard_tenant.py", "tenant-a", "--config-dir", str(root), *extra)
        assert r.returncode == 2, (extra, r.stdout + r.stderr)
        assert "Pre-check 通過" not in r.stdout and "正在執行下架" not in r.stdout, r.stdout
        assert "✅ 設定檔案:" in r.stdout, r.stdout  # the report is still shown
        assert (root / "tenant-a.yaml").is_symlink()


@pytest.mark.parametrize("bad", ["Bad_Id", "../x", "tenant-a/", ""])
def test_offboard_tenant_refuses_an_invalid_tenant_id_before_reading(tmp_path, bad):
    # ADR-035: rc 2 naming the rule; nothing read (no da-guard is even
    # needed), nothing deleted.
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    before = _bytes(root)
    r = _run("ops/offboard_tenant.py", bad, "--config-dir", str(root), "--execute",
             "--skip-state-check", env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "不合法的 tenant id" in r.stderr and "da-guard" not in r.stderr, r.stderr
    assert r.stdout == "" and _bytes(root) == before


def test_offboard_tenant_deletes_none_when_one_carrier_declares_another(tmp_path):
    # One of the files that declare tenant-a also declares tenant-b: the
    # pre-check's rule (deleting it would offboard tenant-b too) refuses the
    # run, naming the file — exit 1 as for the single-file case — and
    # neither file is deleted.
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "other.yaml": "tenants:\n  tenant-a: {}\n  tenant-b: {}\n"})
    before = _bytes(root)
    r = _ob(root)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "other.yaml 另宣告了 tenant-b" in r.stdout, r.stdout
    assert _bytes(root) == before


def test_offboard_tenant_refuses_a_file_given_another_tenant_after_the_precheck(
        tmp_path, monkeypatch, capsys):
    # #3206 (CodeRabbit on #3363): the pre-check's dry run and --execute's
    # write are two da-guard runs. A file that gains a declaration of
    # tenant-b between them must not be deleted: each delete carries
    # `tenants: [tenant-a]`, which da-guard judges on the bytes it reads for
    # the real write — declares_mismatch, rc 2, the file named, both files
    # kept. Real da-guard both times; only the edit in between is the hook.
    import _lib_confedit
    import offboard_tenant as ot
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n",
                                       "team-eu/tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    real, calls = _lib_confedit.write_all, []

    def hooked(r, entries, **kw):
        calls.append([dict(e) for e in entries])
        assert all(e.get("tenants") == ["tenant-a"] for e in entries), entries
        out = real(r, entries, **kw)
        if kw.get("dry_run"):  # after the pre-check, before --execute
            (root / "team-eu" / "tenant-a.yaml").write_text(
                "tenants:\n  tenant-a: {}\n  tenant-b: {}\n", encoding="utf-8")
        return out
    monkeypatch.setattr(_lib_confedit, "write_all", hooked)
    monkeypatch.setattr(sys, "argv", ["offboard_tenant.py", "tenant-a", "--config-dir", str(root),
                                      "--execute", "--skip-state-check"])
    with pytest.raises(SystemExit) as exc:
        ot.main()
    assert exc.value.code == 2
    assert len(calls) == 2 and calls[0] == calls[1], calls  # the dry run is the same batch
    out = capsys.readouterr()
    assert "offboard_tenant: ERROR: team-eu/tenant-a.yaml: declares_mismatch:" in out.err, out.err
    assert "tenant-b" in out.err and "已刪除" not in out.out, out.err
    assert sorted(_bytes(root)) == ["team-eu/tenant-a.yaml", "tenant-a.yaml"]


def test_offboard_tenant_a_delete_that_worsens_the_tree_deletes_nothing(tmp_path, monkeypatch,
                                                                        capsys):
    # The batch is judged once with the file gone (worsens_tree): every place
    # named, rc 2, nothing deleted. ⚠️ da-guard's answer is stubbed here — a
    # flat tenant carrier whose pre-check passes has no real tree its removal
    # worsens that this test could build; Go's own rule is pinned in
    # write_replace_delete_test.go and tests/ops/test_lib_confedit.py
    # (`test_deleting_the_selected_carrier_is_tree_worsened`).
    import _lib_confedit
    import offboard_tenant as ot
    root = _tree(tmp_path / "conf.d", {"tenant-a.yaml": "tenants:\n  tenant-a: {}\n"})
    before = _bytes(root)
    real = _lib_confedit.write_all

    def worsened(r, entries, **kw):
        assert entries == [{"path": "tenant-a.yaml", "delete": True, "tenants": ["tenant-a"]}]
        if kw.get("dry_run"):  # the pre-check's dry run passes; the real write is refused
            return real(r, entries, **kw)
        raise _lib_confedit.TreeWorsened([{"kind": "readers_lost", "path": "other.yaml",
                                           "message": "read by [exporter] before the call"}])
    monkeypatch.setattr(_lib_confedit, "write_all", worsened)
    monkeypatch.setattr(sys, "argv", ["offboard_tenant.py", "tenant-a", "--config-dir", str(root),
                                      "--execute", "--skip-state-check"])
    with pytest.raises(SystemExit) as exc:
        ot.main()
    assert exc.value.code == 2
    out = capsys.readouterr()
    assert "offboard_tenant: ERROR: other.yaml: worsens_tree: readers_lost:" in out.err, out.err
    assert "已刪除" not in out.out
    assert _bytes(root) == before


# ── E2 review follow-ups ─────────────────────────────────────────────────

def test_scaffold_generate_profile_goes_through_da_guard_write(tmp_path, monkeypatch):
    # `_profiles.yaml` is a conf.d file: written by `da-guard write`; a value
    # the edit contract has no type for (inf) is not written (#3206 10c-3).
    import scaffold_tenant as st
    real = st.generate_profile
    monkeypatch.setattr(st, "generate_profile", lambda name, dbs, tier="prod": {
        "profiles": {name: {**real(name, dbs, tier=tier)["profiles"][name],
                            "probe_key": float("inf")}}})
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
                          # JSON `Infinity`: a value with no contract type.
                          "db-b": {"receiver_type": "webhook", "group_wait": float("inf")}},
    }), encoding="utf-8")
    out = tmp_path / "out"
    r = _run("ops/scaffold_tenant.py", "--from-onboard", str(hints), "-o", str(out))
    assert r.returncode == 2, r.stdout[-400:] + r.stderr[-800:]
    assert "db-b.yaml" in r.stderr and "group_wait" in r.stderr, r.stderr
    assert not out.exists() or _bytes(out) == {}, sorted(_bytes(out))


def test_migrate_conf_d_refuses_a_destination_outside_the_tree(tmp_path):
    root = _tree(tmp_path / "conf.d", {
        "tenant-a.yaml": "tenants:\n  tenant-a:\n    _metadata:\n      domain: ..\n"})
    before = _bytes(root)
    for mode in ("--dry-run", "--apply"):
        r = _run("dx/migrate_conf_d.py", "--conf-d", str(root), mode)
        assert r.returncode == 2, r.stdout + r.stderr
        assert "../tenant-a.yaml" in r.stderr and "inside --config-dir" in r.stderr, r.stderr
        assert _bytes(root) == before


def test_threshold_govern_a_failing_da_guard_is_that_tenants_error(tmp_path, monkeypatch):
    import _lib_parse_tree as pt
    import threshold_govern as tg

    def boom(*a, **k):
        raise pt.ParseTreeError("da-guard parse-tree did not finish within 120s")
    monkeypatch.setattr(tg, "edit_live_yaml", boom)
    monkeypatch.setattr(tg, "http_get_json", lambda url, timeout, headers: (
        {"raw_yaml": 'tenants:\n  tenant-a:\n    k1: "10"\n'}, None))
    plan = tg.TenantPlan("tenant-a", [tg.PlannedChange("k1", "10", 15.0, '"15"', 50.0,
                                                       "HIGH", 15.0, "")])
    args = type("A", (), {"tenant_api_url": "http://x", "timeout": 1, "config_dir": str(tmp_path),
                          "identity_email": "", "identity_groups": "", "auth_token": "",
                          "auth_token_file": ""})()
    out = tg.open_governance_pr(plan, args)
    assert out.status == "error" and "verify could not run" in out.message, out
    monkeypatch.setattr(tg, "edit_live_yaml",
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
# seed, and `da-guard write` writes them (one batch per conf.d root). The rows run `regen_surfaces` on copies of the two real files in a
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
    # number the exporter's `defaults` takes: da-guard write's validation
    # names the file. Neither file is written (the seed's change alone would
    # pass: all or none).
    lib, dev, seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE),
                                  _stale(_SEED_REL, *_SEED_STALE))
    before = _bytes(tmp_path / "repo")
    doc = lib.build_registry_doc()
    doc["keys"]["container_cpu"]["value"] = float("inf")
    with pytest.raises(lib.ConfdWriteRefused) as exc:
        lib.regen_surfaces(doc)
    assert f"{dev}: read_back_mismatch: validation would name" in str(exc.value)
    assert str(seed) not in str(exc.value)
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_does_not_write_an_unreadable_original(tmp_path, monkeypatch):
    # A key written twice outside the generated block: PyYAML kept the last
    # one and the regen wrote; yaml.v3 does not read the text to be written.
    text = _stale(_DEV_REL, *_DEV_STALE) + "flag: 1\nflag: 2\n"
    lib, dev, _seed = _regen_setup(tmp_path, monkeypatch, text)
    before = _bytes(tmp_path / "repo")
    with pytest.raises(lib.ConfdWriteRefused, match="read_back_mismatch.*duplicate_key"):
        lib.regen_surfaces(lib.build_registry_doc())
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_names_every_refused_file(tmp_path, monkeypatch):
    """Both conf.d roots refused: each file named, not only the first one
    (every root is judged before the answer), and nothing written."""
    lib, dev, seed = _regen_setup(
        tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE) + "flag: 1\nflag: 2\n",
        _stale(_SEED_REL, *_SEED_STALE) + "flag: 1\nflag: 2\n")
    before = _bytes(tmp_path / "repo")
    with pytest.raises(lib.ConfdWriteRefused) as exc:
        lib.regen_surfaces(lib.build_registry_doc())
    assert "refused 2 generated conf.d file(s)" in str(exc.value)
    assert f"{dev}: read_back_mismatch" in str(exc.value)
    assert f"{seed}: read_back_mismatch" in str(exc.value)
    assert _bytes(tmp_path / "repo") == before


def test_threshold_registry_regen_writes_through_da_guard_write(tmp_path, monkeypatch):
    """ADR-036 §2: the conf.d files go to `da-guard write` — one batch per
    conf.d root, each file a `replace` of its whole text — asked first as a
    dry run, then the same batch written; no Python write of a conf.d file."""
    import _lib_confedit
    import _lib_python
    lib, dev, seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE),
                                  _stale(_SEED_REL, *_SEED_STALE))
    calls = []
    real = _lib_confedit.write_all

    def spy(root, entries, **kw):
        calls.append((str(root), [{**e, "copy": Path(e["copy"]).read_bytes()} for e in entries],
                      kw.get("dry_run", False)))
        return real(root, entries, **kw)

    monkeypatch.setattr(_lib_confedit, "write_all", spy)
    secure = []
    real_secure = _lib_python.write_text_secure
    monkeypatch.setattr(_lib_python, "write_text_secure",
                        lambda p, *a, **k: (secure.append(str(p)), real_secure(p, *a, **k)))
    lib.regen_surfaces(lib.build_registry_doc())
    want = {str(dev.parent): (REPO / _DEV_REL).read_bytes(),
            str(seed.parent): (REPO / _SEED_REL).read_bytes()}
    assert [(root, dry) for root, _e, dry in calls] == [
        (str(dev.parent), True), (str(seed.parent), True),
        (str(dev.parent), False), (str(seed.parent), False)]
    for root, entries, _dry in calls:
        assert entries == [{"path": "_defaults.yaml", "copy": want[root], "replace": True}]
    assert secure == []
    assert dev.read_bytes() == want[str(dev.parent)]


def test_threshold_registry_regen_keeps_the_file_mode(tmp_path, monkeypatch):
    # A replace keeps the file's mode (the Python write set 0600).
    lib, dev, _seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE))
    dev.chmod(0o644)
    lib.regen_surfaces(lib.build_registry_doc())
    assert dev.read_bytes() == (REPO / _DEV_REL).read_bytes()
    assert dev.stat().st_mode & 0o777 == 0o644


def test_threshold_registry_regen_refuses_a_symlinked_conf_d_file(tmp_path, monkeypatch):
    # The Python write followed the link and wrote its target (in the repo:
    # one outside it is the containment check's).
    lib, dev, _seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE))
    target = tmp_path / "repo" / "elsewhere.yaml"
    target.write_bytes(dev.read_bytes())
    dev.unlink()
    dev.symlink_to(target)
    before = target.read_bytes()
    with pytest.raises(lib.ConfdWriteRefused, match=str(dev)):
        lib.regen_surfaces(lib.build_registry_doc())
    assert target.read_bytes() == before and dev.is_symlink()


def test_threshold_registry_regen_without_da_guard_is_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(TOOLS / "lint"))
    import check_threshold_registry as gate
    lib, _dev, _seed = _regen_setup(tmp_path, monkeypatch, _stale(_DEV_REL, *_DEV_STALE))
    monkeypatch.setenv("DA_GUARD_BINARY", str(tmp_path / "no-such-da-guard"))
    before = _bytes(tmp_path / "repo")
    assert gate.main(["--regen"]) == 2
    err = capsys.readouterr().err
    assert "through `da-guard write`" in err and "nothing was written" in err
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
    assert "read_back_mismatch" in capsys.readouterr().err
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


# ── onboard_platform（Phase 1 的 <tenant>.yaml、Phase 2 的 _defaults 建議；#3206 10c 起由 da-guard write 寫）──

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
    text = (out / "phase1-routing" / "tenant-a.yaml").read_text(encoding="utf-8")
    assert 'group_wait: "30s"' in text, text
    assert text.startswith("# Generated by onboard_platform.py — Phase 1\n"), text
    assert "# Review and merge into conf.d/tenant-a.yaml\n\ntenants:\n" in text, text


def test_onboard_platform_writes_a_number_like_string_as_a_string(tmp_path):
    # #3206 10c: the text is Go's. The string '1e3' (once dumped plain by
    # PyYAML, read by yaml.v3 as !!float, refused) is written double-quoted:
    # it reads back as the string it is.
    r, out = _onb(tmp_path, "'1e3'")
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'group_wait: "1e3"' in (out / "phase1-routing" / "tenant-a.yaml").read_text(
        encoding="utf-8")


def test_onboard_platform_rerun_replaces_its_own_files(tmp_path):
    # A rerun into the same -o overwrites, as it always did: the conf.d
    # files are replaced whole (`replace`), the header written once.
    r, out = _onb(tmp_path, "30s")
    assert r.returncode == 0, r.stdout + r.stderr
    r, out = _onb(tmp_path, "45s")
    assert r.returncode == 0, r.stdout + r.stderr
    text = (out / "phase1-routing" / "tenant-a.yaml").read_text(encoding="utf-8")
    assert 'group_wait: "45s"' in text and "30s" not in text, text
    assert text.count("Phase 1") == 1, text


def test_onboard_platform_does_not_write_a_path_no_reader_reads(tmp_path, capsys):
    # The CLI refuses an invalid tenant id first (ADR-035); a library caller
    # reaches da-guard write, which refuses the hidden path — and, the batch
    # being one, writes not even the other tenant's file.
    import onboard_platform as op
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        op.write_outputs(str(out), phase1_results=(
            {".hidden": {"receiver": {"type": "webhook", "url": "https://h.example/x"}},
             "tenant-a": {"receiver": {"type": "webhook", "url": "https://h.example/a"}}},
            {"dedup_tenants": {}}))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "phase1-routing/.hidden.yaml" in err and "nothing was written" in err, err
    assert not out.exists(), sorted(_bytes(out))  # -o is not even created


def test_onboard_platform_refuses_a_tenant_id_a_comment_cannot_carry(tmp_path, capsys):
    # The tenant id goes into the header comment; a character da-guard's
    # comment refuses is rc 2 naming the field, before any write.
    import onboard_platform as op
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as exc:
        op.write_outputs(str(out), phase1_results=(
            {"tenant a": {"receiver": {"type": "webhook", "url": "https://h.example/x"}}},
            {"dedup_tenants": {}}))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "the tenant id" in err and "U+2028" in err, err
    assert not out.exists(), sorted(_bytes(out))  # -o is not even created


def test_onboard_platform_without_da_guard_is_exit_2(tmp_path):
    r, out = _onb(tmp_path, "30s", env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "da-guard" in r.stderr, r.stderr
    assert not out.exists(), sorted(_bytes(out))  # -o is not even created


def _onb_rules(tmp_path, *rules):
    path = tmp_path / "rules.yml"
    path.write_text("groups:\n- name: g\n  rules:\n" + "".join(
        f"  - alert: {a}\n    expr: {e}\n    labels: {{severity: {s}}}\n"
        for a, e, s in rules), encoding="utf-8")
    out = tmp_path / "out"
    return _run("ops/onboard_platform.py", "--rule-files", str(path), "-o", str(out)), out


def test_onboard_platform_defaults_suggestion_is_numbers_the_exporter_reads(tmp_path):
    # The suggestion is "merge into conf.d/_defaults.yaml"; the exporter decodes
    # `defaults:` into float64, so a quoted '80' dropped the whole block. Put
    # the generated file into a tree as `_defaults.yaml`: da-guard serves it.
    r, out = _onb_rules(tmp_path, ("Deep", "app_queue_depth > 1e3", "warning"),
                        ("Ratio", "app_err_ratio > 0.5", "warning"))
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


def test_onboard_platform_writes_a_default_past_18_digits_as_a_float(tmp_path):
    # 1e20 is the int 100000000000000000000: past the edit contract's 18
    # characters, so it goes as the float it is to the exporter (float64).
    r, out = _onb_rules(tmp_path, ("Deep", "app_queue_depth > 1e20", "warning"))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "app_queue_depth: 100000000000000000000.0\n" in (
        out / "phase2-rules" / "_defaults-suggestion.yaml").read_text(encoding="utf-8")


def test_onboard_platform_critical_tier_is_the_foot_comment(tmp_path):
    # The critical tier stays after `defaults:` (da-guard write's foot
    # comment), its "block above" wording intact; a critical-only estate is
    # a file whose top level is `{}`, the tier still in it.
    r, out = _onb_rules(tmp_path, ("Cpu", "cpu_usage > 80", "warning"),
                        ("CpuHigh", "cpu_usage > 95", "critical"))
    assert r.returncode == 0, r.stdout + r.stderr
    text = (out / "phase2-rules" / "_defaults-suggestion.yaml").read_text(encoding="utf-8")
    assert text.index("defaults:\n") < text.index("# ── Critical tier"), text
    assert "`defaults:` block\n# ABOVE" in text, text
    assert '#     cpu_usage_critical: "95"\n' in text, text
    only = tmp_path / "critical-only"
    only.mkdir()
    r, out = _onb_rules(only, ("CpuHigh", "cpu_usage > 95", "critical"))
    assert r.returncode == 0, r.stdout + r.stderr
    text = (out / "phase2-rules" / "_defaults-suggestion.yaml").read_text(encoding="utf-8")
    assert [ln for ln in text.splitlines() if not ln.startswith("#") and ln] == ["{}"], text
    assert '#     cpu_usage_critical: "95"' in text, text


def test_onboard_platform_leaves_a_non_number_out_of_defaults(tmp_path):
    import onboard_platform as op
    [entry] = op._phase2_confd(([
        {"status": "perfect", "metric_key": "a_b", "severity": "warning",
         "threshold_value": "1.2.3"},
        {"status": "perfect", "metric_key": "c_d", "severity": "warning",
         "threshold_value": "70"}], [], {}))
    assert "a_b: threshold '1.2.3' is not a number" in entry["foot_comment"], entry
    [(key, defaults)] = entry["create"]["map"]
    assert key == "defaults" and [k for k, _v in defaults["map"]] == ["c_d"], entry


# ── migrate_rule（tenant-config.yaml、defaults-snippet.yaml；#3206 10c 起由 da-guard write 寫）──

def _mr(tmp_path, threshold, env=None, *extra):
    rules = tmp_path / "rules.yml"
    rules.write_text("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
                     f"    expr: app_queue_depth > {threshold}\n"
                     "    labels: {severity: warning}\n", encoding="utf-8")
    out = tmp_path / "out"
    return _run("ops/migrate_rule.py", str(rules), *extra, "-o", str(out), env=env), out


def test_migrate_rule_writes_what_reads_back(tmp_path):
    r, out = _mr(tmp_path, "1e3")
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'custom_app_queue_depth: "1e3"' in (out / "tenant-config.yaml").read_text(
        encoding="utf-8")
    assert "custom_app_queue_depth: 1000" in (out / "defaults-snippet.yaml").read_text(
        encoding="utf-8")


def test_migrate_rule_quotes_a_key_holding_a_hash(tmp_path):
    # #3206 10c: the text is Go's. A prefix holding ` #` (once a comment that
    # turned the file into a bare scalar, refused) is a key written
    # double-quoted, read back as itself.
    r, out = _mr(tmp_path, "80", None, "--prefix", "team #")
    assert r.returncode == 0, r.stdout + r.stderr
    assert '"team #app_queue_depth": "80"' in (out / "tenant-config.yaml").read_text(
        encoding="utf-8")


def test_migrate_rule_one_refused_file_writes_neither(tmp_path):
    # The two conf.d files are one batch: `defaults-snippet.yaml` (once
    # written last) refused as a symlink ⇒ `tenant-config.yaml` is not
    # written either, nor any rule pack or report.
    from _platform_fs import symlink_or_skip
    (tmp_path / "elsewhere.yaml").write_text("defaults: {}\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    symlink_or_skip(str(tmp_path / "elsewhere.yaml"), out / "defaults-snippet.yaml")
    before = _bytes(out)
    r, out = _mr(tmp_path, "80")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "defaults-snippet.yaml: symlink" in r.stderr and "nothing was written" in r.stderr, \
        r.stderr
    assert sorted(p.name for p in out.iterdir()) == ["defaults-snippet.yaml"]
    assert _bytes(out) == before


def test_migrate_rule_rerun_replaces_both_files(tmp_path):
    r, out = _mr(tmp_path, "80")
    assert r.returncode == 0, r.stdout + r.stderr
    r, out = _mr(tmp_path, "90")
    assert r.returncode == 0, r.stdout + r.stderr
    tc = (out / "tenant-config.yaml").read_text(encoding="utf-8")
    assert 'custom_app_queue_depth: "90"' in tc and '"80"' not in tc, tc
    assert "custom_app_queue_depth: 90\n" in (out / "defaults-snippet.yaml").read_text(
        encoding="utf-8")


def test_migrate_rule_writes_a_default_past_18_digits_as_a_float(tmp_path):
    # 1e20 is the int 100000000000000000000: past the edit contract's 18
    # characters, it goes as the float it is to the exporter (float64).
    r, out = _mr(tmp_path, "1e20")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "custom_app_queue_depth: 100000000000000000000.0\n" in (
        out / "defaults-snippet.yaml").read_text(encoding="utf-8")


def test_migrate_rule_without_da_guard_is_exit_2(tmp_path):
    r, out = _mr(tmp_path, "80", env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "da-guard" in r.stderr, r.stderr
    assert not out.exists(), sorted(_bytes(out))  # -o is not even created


def test_a_failed_batch_leaves_no_output_directory(tmp_path):
    # The conf.d batch goes first; when it exits 2 (here: no da-guard), the
    # -o directories the run created for it — a nested one too — are gone
    # again, so a failed run leaves nothing behind (as before #3206).
    rules = tmp_path / "rules.yml"
    rules.write_text("groups:\n- name: g\n  rules:\n  - alert: Deep\n"
                     "    expr: app_queue_depth > 80\n", encoding="utf-8")
    nested = tmp_path / "new" / "deeper" / "out"
    r = _run("ops/migrate_rule.py", str(rules), "-o", str(nested),
             env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / "new").exists()
    am = tmp_path / "am.yml"
    am.write_text(_OB_AM.replace("{GW}", "30s"), encoding="utf-8")
    r = _run("ops/onboard_platform.py", "--alertmanager-config", str(am),
             "-o", str(nested), env=_no_da_guard(tmp_path))
    assert r.returncode == 2, r.stdout + r.stderr
    assert not (tmp_path / "new").exists()


def test_migrate_rule_names_a_threshold_with_no_finite_value(tmp_path):
    # 1e999 is inf: once written as `.inf`, it has no finite number the
    # exporter's `defaults:` holds — rc 2 naming the key, nothing written.
    r, out = _mr(tmp_path, "1e999")
    assert r.returncode == 2, r.stdout + r.stderr
    assert ("defaults-snippet.yaml: defaults.custom_app_queue_depth: "
            "the float inf is not a finite number") in r.stderr, r.stderr
    assert not out.exists(), sorted(_bytes(out))


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


def test_generate_tenant_fixture_one_refused_file_writes_none(tmp_path, capsys):
    # #3206 10c-2: one `da-guard write` batch for the whole fixture. Here Go
    # refuses it (the `_defaults.yaml` it would add deselects the
    # `_defaults.yml` already there: the tree gets worse), so not one
    # tenant file is written — only what was there stays.
    import generate_tenant_fixture as gtf
    out = _tree(tmp_path / "conf.d", {"_defaults.yml": "defaults:\n  k: 1\n"})
    before = _bytes(out)
    with pytest.raises(SystemExit) as exc:
        gtf.generate_flat(3, out, True, 42)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "_defaults.yml" in err and "nothing was written" in err, err
    assert _bytes(out) == before


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
