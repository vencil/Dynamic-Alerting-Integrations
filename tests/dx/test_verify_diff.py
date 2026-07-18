"""Tests for scripts/tools/dx/verify_diff.py（測試 ROI 第六輪 W6-E）。

Coverage:
  - rules YAML schema 驗證（justification 必填 / 未知欄位 / version）
  - glob 語意（** 跨層、* 不跨 /）
  - 映射建置：AST import 反查 / 文字路徑掃描 / 段接式路徑（含兩層變數
    dataflow）/ special basename
  - 選擇引擎：import / identity / dir-rule(pytest 與 external) /
    always-run additive / full-run trigger / safe-ignore / fail-closed /
    suite 去重
  - 映射保鮮：digest 陳舊偵測 + 現場重生；--check 未映射 fail 與例外表、
    殭屍例外條目
  - CLI：無輸入 → exit 2；--json 單一 JSON 文件；--check exit code
  - 真實 repo 煙霧測試：bump_docs.py → test_bump_docs.py 映射存在
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx",
))
import verify_diff as vd  # noqa: E402


# ============================================================
# Synthetic repo fixture
# ============================================================

def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def synth_repo(tmp_path):
    """最小合成 repo：一個 dx 工具 + 各式引用型態的測試。"""
    root = tmp_path / "repo"
    _write(root / "scripts" / "tools" / "dx" / "mytool.py", "X = 1\n")
    _write(root / "scripts" / "tools" / "dx" / "orphantool.py", "Y = 2\n")
    _write(root / "scripts" / "tools" / "_lib_exitcodes.py", "EXIT_OK = 0\n")
    _write(root / "rule-packs" / "pack-a.yaml", "groups: []\n")
    _write(root / "helm" / "chart" / "values.yaml", "a: 1\n")
    _write(root / "tests" / "conftest.py", "import sys\n")
    # import 反查
    _write(root / "tests" / "dx" / "test_mytool.py",
           "import mytool\n\ndef test_x():\n    assert mytool.X == 1\n")
    # 文字路徑掃描（literal）
    _write(root / "tests" / "ops" / "test_pack.py",
           'P = "rule-packs/pack-a.yaml"\n\ndef test_p():\n    assert P\n')
    # 段接式路徑（兩層變數 dataflow）
    _write(root / "tests" / "ops" / "test_joined.py",
           "from pathlib import Path\n"
           "_ROOT = Path(__file__).resolve().parents[2]\n"
           '_DX = _ROOT / "scripts" / "tools" / "dx"\n'
           "def test_j():\n"
           '    assert (_DX / "mytool.py")\n')
    # 什麼都不引用（--check 的未映射案例）
    _write(root / "tests" / "ops" / "test_orphan.py",
           "def test_nothing():\n    assert True\n")
    return root


def _rules(**over):
    """最小合法 rules dict（select_tests 直接吃 dict）。"""
    base = {
        "version": 1,
        "always_run": {
            "trigger": "scripts/tools/**/*.py",
            "tests": [{"path": "tests/shared/test_sweep.py",
                       "justification": "glob 收集所有工具"}],
        },
        "full_run_triggers": [
            {"pattern": "tests/conftest.py", "justification": "全套 fixture"},
            {"pattern": "scripts/tools/_lib_*.py", "justification": "共用 lib"},
        ],
        "dir_rules": [
            {"name": "helm", "source": "helm/**", "suite": "tests/helm",
             "runner": "pytest", "justification": "chart guard"},
            {"name": "contract", "source": "tests/contract/**",
             "suite": "tests/contract", "runner": "make contract-test",
             "justification": "schemathesis 非 pytest"},
        ],
        "safe_ignore": [
            {"pattern": "**/*.md", "justification": "無 pytest 面"},
        ],
        "overrides": [],
        "unmapped_test_ok": [],
    }
    base.update(over)
    return base


# ============================================================
# rules YAML schema
# ============================================================

class TestRulesSchema:
    def test_real_rules_file_loads(self):
        rules = vd.load_rules(vd.DEFAULT_RULES_PATH)
        assert rules["version"] == 1
        assert rules["always_run"]["tests"]

    def test_missing_justification_rejected(self, tmp_path):
        p = _write(tmp_path / "r.yaml", yaml.safe_dump({
            "version": 1,
            "always_run": {"trigger": "x", "tests": [{"path": "t.py"}]},
        }, allow_unicode=True))
        with pytest.raises(vd.RulesError, match="justification"):
            vd.load_rules(p)

    def test_unknown_key_rejected(self, tmp_path):
        p = _write(tmp_path / "r.yaml", yaml.safe_dump(
            {"version": 1, "always_run": {"trigger": "x", "tests": []},
             "totally_new_key": []}))
        with pytest.raises(vd.RulesError, match="totally_new_key"):
            vd.load_rules(p)

    def test_bad_version_rejected(self, tmp_path):
        p = _write(tmp_path / "r.yaml", "version: 99\n")
        with pytest.raises(vd.RulesError, match="version"):
            vd.load_rules(p)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(vd.RulesError, match="不存在"):
            vd.load_rules(tmp_path / "nope.yaml")


# ============================================================
# glob 語意
# ============================================================

class TestGlob:
    @pytest.mark.parametrize("pattern,path,expect", [
        ("helm/**", "helm/chart/values.yaml", True),
        ("helm/**", "helm", False),
        ("helm/**", "helmet/x", False),
        ("**/*.md", "docs/a/b.md", True),
        ("**/*.md", "README.md", True),
        ("*.md", "docs/a.md", False),          # * 不跨 /
        ("scripts/tools/*/_*.py", "scripts/tools/dx/_atomic_write.py", True),
        ("scripts/tools/*/_*.py", "scripts/tools/dx/tool.py", False),
        ("scripts/tools/_lib_*.py", "scripts/tools/_lib_io.py", True),
        ("scripts/tools/_lib_*.py", "scripts/tools/ops/_lib_io.py", False),
        ("scripts/tools/**/*.py", "scripts/tools/ops/a.py", True),
        ("scripts/tools/**/*.py", "scripts/tools/a.py", True),  # ** 可為空
    ])
    def test_glob_semantics(self, pattern, path, expect):
        assert bool(vd._glob_to_re(pattern).match(path)) is expect


# ============================================================
# 映射建置
# ============================================================

class TestBuildMap:
    def test_import_reverse_lookup(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        assert "tests/dx/test_mytool.py" in \
            vmap["import_map"]["scripts/tools/dx/mytool.py"]

    def test_text_ref_literal(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        assert "tests/ops/test_pack.py" in \
            vmap["text_map"]["rule-packs/pack-a.yaml"]

    def test_joined_path_two_level_dataflow(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        assert "tests/ops/test_joined.py" in \
            vmap["text_map"]["scripts/tools/dx/mytool.py"]

    def test_joined_path_dir_not_collected(self, synth_repo):
        """段接結果是目錄 → 不收（防 tmp fixture 假樹過選）。"""
        vmap = vd.build_map(synth_repo)
        assert "scripts/tools/dx" not in vmap["text_map"]

    def test_orphan_tool_not_mapped(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        assert "scripts/tools/dx/orphantool.py" not in vmap["import_map"]
        assert "scripts/tools/dx/orphantool.py" not in vmap["text_map"]

    def test_digest_changes_when_test_edited(self, synth_repo):
        d1 = vd.build_map(synth_repo)["source_digest"]
        _write(synth_repo / "tests" / "dx" / "test_mytool.py",
               "import mytool\n\ndef test_x2():\n    assert mytool.X == 1\n")
        d2 = vd.build_map(synth_repo)["source_digest"]
        assert d1 != d2


# ============================================================
# 選擇引擎
# ============================================================

class TestSelect:
    def test_import_hit(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["scripts/tools/dx/mytool.py"], vmap, _rules(),
                            synth_repo)
        assert r["mode"] == "subset"
        assert "tests/dx/test_mytool.py" in r["selected"]
        assert any(x.startswith("import") for x in
                   r["selected"]["tests/dx/test_mytool.py"])
        # always-run additive
        assert "tests/shared/test_sweep.py" in r["selected"]

    def test_identity(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["tests/ops/test_orphan.py"], vmap, _rules(),
                            synth_repo)
        assert r["mode"] == "subset"
        assert "tests/ops/test_orphan.py" in r["selected"]

    def test_full_run_trigger(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["tests/conftest.py"], vmap, _rules(), synth_repo)
        assert r["mode"] == "full"
        assert r["full_run_triggers_hit"][0]["pattern"] == "tests/conftest.py"

    def test_shared_lib_full_run(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["scripts/tools/_lib_exitcodes.py"], vmap,
                            _rules(), synth_repo)
        assert r["mode"] == "full"

    def test_fail_closed_unmapped(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["mystery/unknown.bin"], vmap, _rules(),
                            synth_repo)
        assert r["mode"] == "full"
        assert r["unmapped"] == ["mystery/unknown.bin"]

    def test_orphan_tool_fail_closed_despite_always_run(self, synth_repo):
        """沒有專屬測試的工具變更：always-run 有選、但仍 fail-closed 全跑。"""
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["scripts/tools/dx/orphantool.py"], vmap,
                            _rules(), synth_repo)
        assert r["mode"] == "full"
        assert "scripts/tools/dx/orphantool.py" in r["unmapped"]
        assert "tests/shared/test_sweep.py" in r["selected"]

    def test_safe_ignore(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["docs/readme-like.md"], vmap, _rules(),
                            synth_repo)
        assert r["mode"] == "empty"
        assert r["ignored"][0]["path"] == "docs/readme-like.md"

    def test_dir_rule_pytest_suite(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["helm/chart/values.yaml"], vmap, _rules(),
                            synth_repo)
        assert r["mode"] == "subset"
        assert "tests/helm" in r["selected"]

    def test_dir_rule_external_runner(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests(["tests/contract/run_contract_tests.py"], vmap,
                            _rules(), synth_repo)
        assert r["mode"] == "subset"
        assert not r["selected"]
        assert r["external_suites"]["tests/contract"]["runner"] == \
            "make contract-test"

    def test_suite_dedupe_absorbs_files(self, synth_repo):
        """已選 suite 目錄時，其下單檔合併進 suite（pytest 跑目錄即含檔）。"""
        vmap = vd.build_map(synth_repo)
        rules = _rules(dir_rules=[
            {"name": "ops", "source": "rule-packs/**", "suite": "tests/ops",
             "runner": "pytest", "justification": "x"},
        ])
        r = vd.select_tests(
            ["rule-packs/pack-a.yaml", "tests/ops/test_orphan.py"],
            vmap, rules, synth_repo)
        assert "tests/ops" in r["selected"]
        assert "tests/ops/test_orphan.py" not in r["selected"]

    def test_windows_paths_normalized(self, synth_repo):
        vmap = vd.build_map(synth_repo)
        r = vd.select_tests([r"scripts\tools\dx\mytool.py"], vmap, _rules(),
                            synth_repo)
        assert "tests/dx/test_mytool.py" in r["selected"]


# ============================================================
# pytest argv 組裝（sequential vs -n auto）
# ============================================================

class TestPytestArgv:
    def _result(self, n):
        return {"mode": "subset",
                "selected": {f"tests/dx/test_{i}.py": ["r"] for i in range(n)},
                "external_suites": {}}

    def test_small_set_sequential(self):
        argv = vd.build_pytest_argv(self._result(3), xdist_threshold=10)
        assert "-n" not in argv

    def test_large_set_uses_xdist(self):
        argv = vd.build_pytest_argv(self._result(11), xdist_threshold=10)
        assert argv[-2:] == ["-n", "auto"]

    def test_full_mode_runs_whole_suite(self):
        argv = vd.build_pytest_argv(
            {"mode": "full", "selected": {}, "external_suites": {}},
            xdist_threshold=10)
        assert "tests/" in argv and "--ignore=tests/federation-e2e" in argv


# ============================================================
# 映射保鮮（staleness + --check）
# ============================================================

class TestFreshness:
    def test_stale_map_rebuilt_with_warning(self, synth_repo, tmp_path,
                                            capsys):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        _write(synth_repo / "tests" / "dx" / "test_new.py",
               "import mytool\n\ndef test_n():\n    assert True\n")
        vmap, stale = vd.load_or_rebuild_map(synth_repo, map_path)
        assert stale is True
        assert "陳舊" in capsys.readouterr().err
        assert "tests/dx/test_new.py" in vmap["tests_scanned"]

    def test_fresh_map_used_as_is(self, synth_repo, tmp_path):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        _, stale = vd.load_or_rebuild_map(synth_repo, map_path)
        assert stale is False

    def test_check_flags_unmapped_test(self, synth_repo, tmp_path):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        problems, _ = vd.check_map(synth_repo, map_path, _rules())
        assert any("test_orphan.py" in p for p in problems)

    def test_check_respects_exception_table(self, synth_repo, tmp_path):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        rules = _rules(unmapped_test_ok=[
            {"test": "tests/ops/test_orphan.py", "justification": "純語意測試"},
        ])
        problems, _ = vd.check_map(synth_repo, map_path, rules)
        assert not any("未映射" in p for p in problems)

    def test_check_flags_zombie_exception(self, synth_repo, tmp_path):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        rules = _rules(unmapped_test_ok=[
            {"test": "tests/ops/test_orphan.py", "justification": "ok"},
            {"test": "tests/gone/test_gone.py", "justification": "殭屍"},
        ])
        problems, _ = vd.check_map(synth_repo, map_path, rules)
        assert any("殭屍" in p for p in problems)

    def test_check_flags_stale_map_file(self, synth_repo, tmp_path):
        map_path = tmp_path / "map.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        _write(synth_repo / "tests" / "dx" / "test_more.py",
               "import mytool\n\ndef test_m():\n    assert True\n")
        problems, _ = vd.check_map(synth_repo, map_path, _rules())
        assert any("陳舊" in p for p in problems)


# ============================================================
# CLI（main）
# ============================================================

@pytest.fixture
def synth_cli(synth_repo, tmp_path):
    """合成 repo 的 CLI 參數組（rules YAML 落地 + map 落地）。"""
    rules_path = _write(tmp_path / "rules.yaml",
                        yaml.safe_dump(_rules(), allow_unicode=True))
    map_path = tmp_path / "map.json"
    vd.write_map(vd.build_map(synth_repo), map_path)
    return ["--repo-root", str(synth_repo), "--rules", str(rules_path),
            "--map", str(map_path)]


class TestCli:
    def _run_main(self, cli_argv, args):
        cli_argv("verify_diff.py", *args)
        with pytest.raises(SystemExit) as e:
            vd.main()
        return e.value.code

    def test_no_input_is_caller_error(self, cli_argv, synth_cli):
        assert self._run_main(cli_argv, synth_cli) == 2

    def test_json_single_document(self, cli_argv, synth_cli, capsys):
        code = self._run_main(
            cli_argv, [*synth_cli, "--json", "scripts/tools/dx/mytool.py"])
        assert code == 0
        doc = json.loads(capsys.readouterr().out)  # 全文 parse＝單一文件
        assert doc["mode"] == "subset"
        assert "tests/dx/test_mytool.py" in doc["selected"]

    def test_dry_run_text_report(self, cli_argv, synth_cli, capsys):
        code = self._run_main(
            cli_argv, [*synth_cli, "--dry-run", "tests/conftest.py"])
        assert code == 0
        assert "全跑" in capsys.readouterr().out

    def test_check_ok_after_exception(self, cli_argv, synth_repo, tmp_path,
                                      capsys):
        rules_path = _write(
            tmp_path / "rules2.yaml",
            yaml.safe_dump(_rules(unmapped_test_ok=[
                {"test": "tests/ops/test_orphan.py",
                 "justification": "純語意測試"}]), allow_unicode=True))
        map_path = tmp_path / "map2.json"
        vd.write_map(vd.build_map(synth_repo), map_path)
        code = self._run_main(cli_argv, [
            "--repo-root", str(synth_repo), "--rules", str(rules_path),
            "--map", str(map_path), "--check"])
        assert code == 0

    def test_check_fails_on_unmapped(self, cli_argv, synth_cli):
        assert self._run_main(cli_argv, [*synth_cli, "--check"]) == 1

    def test_bad_rules_is_caller_error(self, cli_argv, synth_repo, tmp_path):
        bad = _write(tmp_path / "bad.yaml", "version: 7\n")
        code = self._run_main(cli_argv, [
            "--repo-root", str(synth_repo), "--rules", str(bad), "x.py"])
        assert code == 2

    def test_write_map_roundtrip(self, cli_argv, synth_repo, tmp_path):
        rules_path = _write(tmp_path / "r3.yaml",
                            yaml.safe_dump(_rules(), allow_unicode=True))
        map_path = tmp_path / "m3.json"
        code = self._run_main(cli_argv, [
            "--repo-root", str(synth_repo), "--rules", str(rules_path),
            "--map", str(map_path), "--write-map"])
        assert code == 0
        with open(map_path, encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk["version"] == vd.MAP_VERSION


# ============================================================
# 真實 repo 煙霧測試
# ============================================================

class TestRealRepoSmoke:
    def test_bump_docs_maps_to_its_test(self):
        vmap = vd.build_map(vd.REPO_ROOT)
        assert "tests/dx/test_bump_docs.py" in \
            vmap["import_map"]["scripts/tools/dx/bump_docs.py"]

    def test_repo_check_is_green(self):
        """映射檔新鮮 + 全 test 檔可達（--check 的 repo 守門）。"""
        rules = vd.load_rules(vd.DEFAULT_RULES_PATH)
        problems, _ = vd.check_map(vd.REPO_ROOT, vd.DEFAULT_MAP_PATH, rules)
        assert problems == [], problems
