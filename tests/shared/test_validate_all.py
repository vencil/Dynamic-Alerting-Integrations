"""test_validate_all.py — validate_all.py 測試 (pure helpers + smart-detect + main CLI).

pytest style：使用 plain assert + conftest fixtures。

驗證:
  1. _extract_detail() — 工具輸出摘要抽取
  2. _status_symbol() — 狀態符號映射
  3. _format_time() — 時間格式化
  4. _detect_changed_checks() — 快照差異偵測
  5. _compare_baseline() — 基線比對輸出
  6. _snapshot_mtimes() — 檔案 mtime 快照
  7. TOOLS / FIX_COMMANDS 常數一致性
  8. WATCH_TRIGGERS 覆蓋率
  9. _send_notification() — 跨平台桌面通知

Merged from previous _extended split (PR test-refactor sweep) — TestSmartDetect
and TestMainExtended classes appended at the bottom cover _smart_detect() git-
diff selection + main() CLI flag matrix (--parallel/--baseline/--compare/--fix/
--profile/--notify/--smart/--diff-report).
"""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import validate_all as va
from validate_all import (
    _extract_detail,
    _run_one,
    _send_notification,
    _status_symbol,
    _format_time,
    _detect_changed_checks,
    _compare_baseline,
    _snapshot_mtimes,
    TOOLS,
    FIX_COMMANDS,
    WATCH_TRIGGERS,
)


# ============================================================
# _extract_detail
# ============================================================
class TestExtractDetail:
    """_extract_detail() 工具輸出摘要提取。"""

    def test_returns_last_meaningful_line(self):
        """回傳最後一行有意義的文字。"""
        output = "line1\nline2\nAll checks passed.\n"
        assert _extract_detail(output) == "All checks passed."

    def test_skips_separator_lines(self):
        """跳過 === 分隔線。"""
        output = "Result summary\n=== END ===\n"
        assert _extract_detail(output) == "Result summary"

    def test_empty_output(self):
        """空輸出回傳空字串。"""
        assert _extract_detail("") == ""
        assert _extract_detail("   \n  \n") == ""

    def test_truncates_long_line(self):
        """超過 80 字元的行被截斷。"""
        long_line = "x" * 100
        assert len(_extract_detail(long_line)) == 80

    def test_only_separator_lines(self):
        """全部為分隔線時回傳空字串。"""
        assert _extract_detail("=== header ===\n=== footer ===\n") == ""

    def test_multiline_with_trailing_blanks(self):
        """忽略尾部空白行，取最後有意義行。"""
        output = "first\nsecond\n\n\n"
        assert _extract_detail(output) == "second"


# ============================================================
# _status_symbol / _format_time
# ============================================================
class TestFormatHelpers:
    """格式化 helper 函式測試。"""

    def test_pass_symbol(self):
        assert _status_symbol("pass") == "✓"

    def test_fail_symbol(self):
        assert _status_symbol("fail") == "✗"

    def test_error_symbol(self):
        assert _status_symbol("error") == "⊘"

    def test_unknown_symbol(self):
        assert _status_symbol("unknown") == "⊘"

    def test_format_time(self):
        assert _format_time(1.234) == "1.2s"
        assert _format_time(0.0) == "0.0s"
        assert _format_time(10.567) == "10.6s"


# ============================================================
# _detect_changed_checks
# ============================================================
class TestDetectChangedChecks:
    """_detect_changed_checks() 檔案變更偵測測試。"""

    def test_docs_change_triggers_doc_checks(self):
        """docs/ 目錄變更觸發文件相關 check。"""
        old = {"docs/guide.md": 1000}
        new = {"docs/guide.md": 2000}
        affected = _detect_changed_checks(old, new)
        assert "links" in affected
        assert "versions" in affected

    def test_rule_packs_change(self):
        """rule-packs/ 目錄變更觸發 rule pack 相關 check。"""
        old = {"rule-packs/mariadb.yaml": 1000}
        new = {"rule-packs/mariadb.yaml": 2000}
        affected = _detect_changed_checks(old, new)
        assert "alerts" in affected
        assert "platform_data" in affected

    def test_no_change_returns_empty(self):
        """無檔案變更回傳空 list。"""
        snap = {"docs/guide.md": 1000}
        assert _detect_changed_checks(snap, snap) == []

    def test_deleted_file_detected(self):
        """刪除的檔案也能偵測。"""
        old = {"docs/old.md": 1000}
        new = {}
        affected = _detect_changed_checks(old, new)
        assert len(affected) > 0

    def test_new_file_detected(self):
        """新增的檔案也能偵測。"""
        old = {}
        new = {"docs/new.md": 1000}
        affected = _detect_changed_checks(old, new)
        assert len(affected) > 0

    def test_scripts_tools_change(self):
        """scripts/tools/ 變更觸發 tool_map check。"""
        old = {"scripts/tools/ops/new_tool.py": 1000}
        new = {"scripts/tools/ops/new_tool.py": 2000}
        affected = _detect_changed_checks(old, new)
        assert "tool_map" in affected

    def test_changelog_change(self):
        """CHANGELOG.md 變更觸發 changelog check。"""
        old = {"CHANGELOG.md": 1000}
        new = {"CHANGELOG.md": 2000}
        affected = _detect_changed_checks(old, new)
        assert "changelog" in affected

    def test_docs_assets_triggers_platform_data(self):
        """docs/assets/ 變更觸發 platform_data 與 tool_consistency 檢查。"""
        old = {"docs/assets/data.json": 1000}
        new = {"docs/assets/data.json": 2000}
        affected = _detect_changed_checks(old, new)
        assert "platform_data" in affected
        assert "tool_consistency" in affected

    def test_unmatched_file_runs_all(self):
        """未匹配任何 WATCH_TRIGGERS 的檔案變更回傳所有檢查。"""
        old = {}
        new = {"some_random_file.txt": 1000}
        affected = _detect_changed_checks(old, new)
        all_names = sorted(n for n, _, _, _ in TOOLS)
        assert affected == all_names

    def test_result_is_sorted(self):
        """回傳結果按字母排序。"""
        old = {"docs/a.md": 1000, "rule-packs/b.yaml": 1000}
        new = {"docs/a.md": 2000, "rule-packs/b.yaml": 2000}
        affected = _detect_changed_checks(old, new)
        assert affected == sorted(affected)


# ============================================================
# _compare_baseline
# ============================================================

class TestCompareBaseline:
    """_compare_baseline() 基線比對輸出。"""

    def test_no_baseline_file(self, capsys, tmp_path, monkeypatch):
        """無基線檔案顯示警告。"""
        monkeypatch.setattr(va, "BASELINE_FILE", tmp_path / "nonexistent.json")
        _compare_baseline({"results": {}, "passed": 0, "failed": 0})
        err = capsys.readouterr().err
        assert "No baseline file found" in err

    def test_regression_detected(self, capsys, tmp_path, monkeypatch):
        """偵測 pass → fail 回歸。"""
        baseline = {
            "results": {"links": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"links": {"status": "fail", "elapsed": 1.0}},
            "passed": 0, "failed": 1,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "Regressions" in err
        assert "links" in err

    def test_improvement_detected(self, capsys, tmp_path, monkeypatch):
        """偵測 fail → pass 改善。"""
        baseline = {
            "results": {"versions": {"status": "fail", "elapsed": 1.0}},
            "passed": 0, "failed": 1,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"versions": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "Improvements" in err
        assert "versions" in err

    def test_timing_warning(self, capsys, tmp_path, monkeypatch):
        """偵測 >20% 效能衰退警告。"""
        baseline = {
            "results": {"links": {"status": "pass", "elapsed": 10.0}},
            "passed": 1, "failed": 0,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"links": {"status": "pass", "elapsed": 15.0}},
            "passed": 1, "failed": 0,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "Timing warnings" in err

    def test_no_regressions_shows_ok(self, capsys, tmp_path, monkeypatch):
        """無回歸時顯示 No regressions detected。"""
        baseline = {
            "results": {"links": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"links": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "No regressions detected" in err

    def test_new_check_in_current(self, capsys, tmp_path, monkeypatch):
        """Current 有新 check（baseline 無）不算回歸。"""
        baseline = {
            "results": {},
            "passed": 0, "failed": 0,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"new_check": {"status": "pass", "elapsed": 0.5}},
            "passed": 1, "failed": 0,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "Regressions" not in err

    def test_timing_no_warning_for_fast_checks(self, capsys, tmp_path, monkeypatch):
        """基線 < 0.5s 的 check 不觸發效能警告（即使倍增）。"""
        baseline = {
            "results": {"glossary": {"status": "pass", "elapsed": 0.2}},
            "passed": 1, "failed": 0,
        }
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

        current = {
            "results": {"glossary": {"status": "pass", "elapsed": 0.5}},
            "passed": 1, "failed": 0,
        }
        _compare_baseline(current)
        err = capsys.readouterr().err
        assert "Timing warnings" not in err


# ============================================================
# _snapshot_mtimes
# ============================================================

class TestSnapshotMtimes:
    """_snapshot_mtimes() 檔案 mtime 快照。"""

    def test_captures_md_files_in_docs(self, tmp_path):
        """擷取 docs/ 下的 .md 檔案。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "guide.md").write_text("hello", encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert "docs/guide.md" in snap

    def test_ignores_non_watched_extensions(self, tmp_path):
        """忽略非觀察副檔名（如 .png）。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "image.png").write_bytes(b"\x89PNG")
        snap = _snapshot_mtimes(tmp_path)
        assert "docs/image.png" not in snap

    def test_captures_root_changelog(self, tmp_path):
        """擷取根目錄的 CHANGELOG.md。"""
        (tmp_path / "CHANGELOG.md").write_text("# log", encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert "CHANGELOG.md" in snap

    def test_empty_repo(self, tmp_path):
        """空目錄回傳空字典。"""
        snap = _snapshot_mtimes(tmp_path)
        assert snap == {}

    def test_captures_yaml_in_rule_packs(self, tmp_path):
        """擷取 rule-packs/ 下的 .yaml 檔案。"""
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        (rp / "mariadb.yaml").write_text("groups: []", encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert "rule-packs/mariadb.yaml" in snap

    def test_captures_py_in_scripts_tools(self, tmp_path):
        """擷取 scripts/tools/ 下的 .py 檔案。"""
        st = tmp_path / "scripts" / "tools"
        st.mkdir(parents=True)
        (st / "helper.py").write_text("pass", encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert "scripts/tools/helper.py" in snap

    def test_captures_jsx_in_docs(self, tmp_path):
        """擷取 docs/ 下的 .jsx 檔案。"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "widget.jsx").write_text("export default () => null;",
                                         encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert "docs/widget.jsx" in snap

    def test_mtime_is_float(self, tmp_path):
        """快照值為 float 型別。"""
        (tmp_path / "CHANGELOG.md").write_text("# log", encoding="utf-8")
        snap = _snapshot_mtimes(tmp_path)
        assert isinstance(snap["CHANGELOG.md"], float)


# ============================================================
# Constants integrity
# ============================================================
class TestConstantsIntegrity:
    """常數完整性驗證。"""

    def test_tools_not_empty(self):
        """TOOLS 列表不為空。"""
        assert len(TOOLS) > 0

    def test_tools_have_four_fields(self):
        """每個 TOOL entry 包含 4 個欄位。"""
        for tool in TOOLS:
            assert len(tool) == 4, f"Tool {tool[0]} has {len(tool)} fields, expected 4"

    def test_tool_names_unique(self):
        """TOOL 名稱不重複。"""
        names = [t[0] for t in TOOLS]
        assert len(names) == len(set(names)), "Duplicate tool names found"

    def test_fix_commands_reference_valid_tools(self):
        """FIX_COMMANDS 中的 key 存在於 TOOLS 中或為已知別名。"""
        tool_names = {t[0] for t in TOOLS}
        for key in FIX_COMMANDS:
            assert key in tool_names, \
                f"FIX_COMMANDS key '{key}' not found in TOOLS"

    def test_watch_triggers_reference_valid_checks(self):
        """WATCH_TRIGGERS 中的 check 名稱存在於 TOOLS 中。"""
        tool_names = {t[0] for t in TOOLS}
        for pattern, checks in WATCH_TRIGGERS.items():
            for check in checks:
                assert check in tool_names, \
                    f"WATCH_TRIGGERS '{pattern}' references unknown check '{check}'"

    def test_tools_scripts_have_py_extension(self):
        """每個 TOOL 的 script_path 以 .py 結尾。"""
        for name, script, _, _ in TOOLS:
            assert script.endswith(".py"), \
                f"Tool '{name}' script '{script}' does not end with .py"

    def test_tools_count_at_least_15(self):
        """TOOLS 至少 15 個驗證工具。"""
        assert len(TOOLS) >= 15

    def test_tools_scripts_in_subdirs(self):
        """TOOLS script 路徑包含子目錄（lint/ 或 dx/）。"""
        for name, script, _, _ in TOOLS:
            assert "/" in script, \
                f"'{name}' script 缺少子目錄前綴: {script}"

    def test_fix_commands_scripts_are_py(self):
        """FIX_COMMANDS 修復腳本皆以 .py 結尾。"""
        for name, cmd in FIX_COMMANDS.items():
            assert cmd[0].endswith(".py"), \
                f"FIX_COMMANDS '{name}' 修復腳本不是 .py: {cmd[0]}"

    def test_tools_args_are_lists(self):
        """TOOLS 每個 entry 的 args 欄位為 list。"""
        for name, _, args, _ in TOOLS:
            assert isinstance(args, list), \
                f"Tool '{name}' args 不是 list: {type(args)}"


# ============================================================
# WATCH_TRIGGERS 覆蓋率
# ============================================================

class TestWatchTriggers:
    """WATCH_TRIGGERS 映射結構驗證。"""

    def test_all_trigger_checks_exist_in_tools(self):
        """WATCH_TRIGGERS 引用的 check 必須存在於 TOOLS。"""
        tool_names = {n for n, _, _, _ in TOOLS}
        for prefix, checks in WATCH_TRIGGERS.items():
            for check in checks:
                assert check in tool_names, \
                    f"WATCH_TRIGGERS['{prefix}'] 引用未知 check: {check}"

    def test_key_paths_end_with_slash_or_are_files(self):
        """WATCH_TRIGGERS key 為目錄（/結尾）或具名檔案。"""
        for key in WATCH_TRIGGERS:
            assert key.endswith("/") or "." in key, \
                f"WATCH_TRIGGERS key 格式不明確: {key}"

    def test_docs_prefix_exists(self):
        """docs/ 前綴存在於 WATCH_TRIGGERS。"""
        assert "docs/" in WATCH_TRIGGERS

    def test_rule_packs_prefix_exists(self):
        """rule-packs/ 前綴存在於 WATCH_TRIGGERS。"""
        assert "rule-packs/" in WATCH_TRIGGERS

    def test_changelog_trigger_exists(self):
        """CHANGELOG.md 觸發存在。"""
        assert "CHANGELOG.md" in WATCH_TRIGGERS


# ============================================================
# _run_one（mock subprocess）
# ============================================================

class TestRunOne:
    """_run_one() 單一驗證工具執行。"""

    def test_pass_returns_pass_status(self, tmp_path):
        """subprocess 正常結束回傳 pass。"""
        script = tmp_path / "ok.py"
        script.write_text("print('All good')", encoding="utf-8")
        name, status, elapsed, detail, output = _run_one(
            "test_check", str(script), [], str(tmp_path))
        assert name == "test_check"
        assert status == "pass"
        assert elapsed >= 0
        assert "All good" in output

    def test_fail_returns_fail_status(self, tmp_path):
        """subprocess 非零退出回傳 fail。"""
        script = tmp_path / "fail.py"
        script.write_text("import sys; print('Error found'); sys.exit(1)",
                          encoding="utf-8")
        name, status, elapsed, detail, output = _run_one(
            "fail_check", str(script), [], str(tmp_path))
        assert status == "fail"
        assert "Error found" in detail

    def test_timeout_returns_error(self, monkeypatch):
        """Timeout 回傳 error 狀態。"""
        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="test", timeout=120)
        monkeypatch.setattr(subprocess, "run", mock_run)
        name, status, elapsed, detail, output = _run_one(
            "slow_check", "dummy.py", [], "/tmp")
        assert status == "error"
        assert "Timeout" in detail

    def test_oserror_returns_error(self, monkeypatch):
        """OSError 回傳 error 狀態。"""
        def mock_run(*args, **kwargs):
            raise OSError("No such file")
        monkeypatch.setattr(subprocess, "run", mock_run)
        name, status, elapsed, detail, output = _run_one(
            "broken_check", "nonexistent.py", [], "/tmp")
        assert status == "error"
        assert "No such file" in detail

    def test_pass_with_args(self, tmp_path):
        """傳遞額外參數到 subprocess。"""
        script = tmp_path / "args.py"
        script.write_text(
            "import sys; print(' '.join(sys.argv[1:]))",
            encoding="utf-8")
        name, status, elapsed, detail, output = _run_one(
            "args_check", str(script), ["--check", "--ci"], str(tmp_path))
        assert status == "pass"
        assert "--check" in output
        assert "--ci" in output


# ============================================================
# main() CLI 模式
# ============================================================

class TestMainCLI:
    """main() CLI 整合測試。"""

    def test_list_mode(self, capsys, monkeypatch, cli_argv):
        """--list 模式列出所有檢查並正常結束。"""
        cli_argv('validate_all', '--list')
        va.main()
        out = capsys.readouterr().out
        # 應包含至少一個 TOOLS 名稱
        assert "links" in out
        assert "versions" in out

    def test_list_mode_shows_all_tools(self, capsys, monkeypatch, cli_argv):
        """--list 模式列出全部 TOOLS。"""
        cli_argv('validate_all', '--list')
        va.main()
        out = capsys.readouterr().out
        for name, _, _, _ in TOOLS:
            assert name in out, f"--list 未顯示 '{name}'"


# ============================================================
# _generate_diff_report()
# ============================================================

class TestGenerateDiffReport:
    """--diff-report 功能測試。"""

    def test_no_fixable_checks(self, tmp_path):
        """No fixable failed checks returns informative message."""
        # Use a check name that's not in FIX_COMMANDS
        result = va._generate_diff_report(
            {"structure": "fail"}, tmp_path, tmp_path)
        assert "No auto-fixable" in result

    def test_fix_produces_diff(self, tmp_path, monkeypatch):
        """Fixable check runs fix and captures git diff."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            if "diff" in cmd:
                mock.stdout = "diff --git a/foo b/foo\n--- a/foo\n+++ b/foo"
            else:
                mock.stdout = ""
            return mock

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "versions" in result
        assert "diff --git" in result
        # Should have called: fix command, git diff, git checkout
        assert len(calls) == 3

    def test_fix_timeout(self, tmp_path, monkeypatch):
        """Timeout during fix is handled gracefully."""
        call_count = [0]

        def mock_run(cmd, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise subprocess.TimeoutExpired(cmd, 60)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            return mock

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "timeout" in result

    def test_no_diff_produced(self, tmp_path, monkeypatch):
        """Fix that produces no diff shows informative message."""
        def mock_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            return mock

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "no diff produced" in result


class TestMainCLI:
    """main() CLI 路徑覆蓋。"""

    def _mock_run_one(self, short_name, script_path, tool_args,
                      project_root):
        """Mock _run_one that always passes."""
        return (short_name, "pass", 0.1, "ok", "output")

    def _mock_run_one_fail(self, short_name, script_path, tool_args,
                           project_root):
        """Mock _run_one that always fails."""
        return (short_name, "fail", 0.2, "error", "failure output")

    def test_list_flag(self, monkeypatch, capsys, cli_argv):
        """--list 列出所有 checks。"""
        cli_argv('validate_all', '--list')
        va.main()
        out = capsys.readouterr().out
        assert "versions" in out or "links" in out

    def test_sequential_json(self, monkeypatch, capsys, cli_argv):
        """Sequential + --json 輸出。"""
        cli_argv('validate_all', '--json', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["passed"] >= 1
        assert data["mode"] == "sequential"

    def test_sequential_text(self, monkeypatch, capsys, cli_argv):
        """Sequential text 輸出。"""
        cli_argv('validate_all', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "Validation Report" in out

    def test_skip_flag(self, monkeypatch, capsys, cli_argv):
        """--skip 跳過指定 check。"""
        cli_argv('validate_all', '--skip', 'versions,links', '--only', 'freshness')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert len(out) > 0

    def test_ci_stops_on_failure(self, monkeypatch, cli_argv):
        """--ci 模式遇到失敗時 exit 1。"""
        cli_argv('validate_all', '--ci', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_fail)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 1

    def test_verbose_flag(self, monkeypatch, capsys, cli_argv):
        """--verbose 顯示完整輸出。"""
        cli_argv('validate_all', '--verbose', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "output" in out or "VERSIONS" in out


# ============================================================
# _send_notification (cross-platform desktop notification)
# ============================================================

class TestSendNotification:
    """_send_notification() 跨平台桌面通知測試。"""

    def _mock_platform(self, monkeypatch, system_name):
        """Helper: mock platform.system() 回傳指定 OS 名稱。"""
        import platform as _platform
        monkeypatch.setattr(_platform, "system", lambda: system_name)

    def test_linux_notify_send(self, monkeypatch):
        """Linux 平台使用 notify-send。"""
        self._mock_platform(monkeypatch, "Linux")
        calls = []
        monkeypatch.setattr(va.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
        _send_notification("Test Title", "Test Message")
        assert len(calls) == 1
        assert calls[0][0] == "notify-send"
        assert "Test Title" in calls[0]
        assert "Test Message" in calls[0]

    def test_macos_osascript(self, monkeypatch):
        """macOS 平台使用 osascript。"""
        self._mock_platform(monkeypatch, "Darwin")
        calls = []
        monkeypatch.setattr(va.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
        _send_notification("Title", "Msg")
        assert len(calls) == 1
        assert calls[0][0] == "osascript"

    def test_windows_powershell(self, monkeypatch):
        """Windows 平台使用 PowerShell。"""
        self._mock_platform(monkeypatch, "Windows")
        calls = []
        monkeypatch.setattr(va.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
        _send_notification("Title", "Msg")
        assert len(calls) == 1
        assert calls[0][0] == "powershell"

    def test_fallback_on_file_not_found(self, monkeypatch, capsys):
        """notify-send 不存在時 fallback 到 terminal bell。"""
        self._mock_platform(monkeypatch, "Linux")
        def raise_fnf(*a, **kw):
            raise FileNotFoundError("notify-send")
        monkeypatch.setattr(va.subprocess, "run", raise_fnf)
        _send_notification("Title", "Msg")
        out = capsys.readouterr().out
        assert "\a" in out

    def test_fallback_on_timeout(self, monkeypatch, capsys):
        """subprocess timeout 時 fallback 到 terminal bell。"""
        self._mock_platform(monkeypatch, "Linux")
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="notify-send", timeout=5)
        monkeypatch.setattr(va.subprocess, "run", raise_timeout)
        _send_notification("Title", "Msg")
        out = capsys.readouterr().out
        assert "\a" in out

    def test_unknown_os_fallback(self, monkeypatch, capsys):
        """未知 OS 直接 fallback 到 terminal bell。"""
        self._mock_platform(monkeypatch, "FreeBSD")
        _send_notification("Title", "Msg")
        out = capsys.readouterr().out
        assert "\a" in out


# ---------------------------------------------------------------------------
# _smart_detect + main() CLI matrix (was test_validate_all_extended.py)
# ---------------------------------------------------------------------------


from validate_all import _smart_detect  # noqa: E402


class TestSmartDetect:
    """_smart_detect() git-diff based check selection."""

    def _mock_git(self, monkeypatch, diff_files="", staged_files="",
                  untracked_files="", fail=False):
        """Mock subprocess.run for git commands."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if fail:
                raise subprocess.TimeoutExpired(cmd, 30)
            result.returncode = 0
            if "diff" in cmd and "--cached" in cmd:
                result.stdout = staged_files
            elif "diff" in cmd:
                result.stdout = diff_files
            elif "ls-files" in cmd:
                result.stdout = untracked_files
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)

    def test_no_changes_returns_empty(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch)
        result = _smart_detect(tmp_path)
        assert result == []

    def test_docs_change_triggers_doc_checks(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="docs/guide.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result
        assert "versions" in result

    def test_rule_packs_change(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="rule-packs/mariadb.yaml\n")
        result = _smart_detect(tmp_path)
        assert "alerts" in result
        assert "platform_data" in result

    def test_unknown_file_runs_all(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="unknown_file.txt\n")
        result = _smart_detect(tmp_path)
        all_names = sorted(n for n, _, _, _ in TOOLS)
        assert result == all_names

    def test_timeout_returns_none(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, fail=True)
        result = _smart_detect(tmp_path)
        assert result is None

    def test_staged_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, staged_files="scripts/tools/ops/new.py\n")
        result = _smart_detect(tmp_path)
        assert "tool_map" in result

    def test_untracked_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, untracked_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "changelog" in result

    def test_result_is_sorted(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/a.md\nrule-packs/b.yaml\n")
        result = _smart_detect(tmp_path)
        assert result == sorted(result)

    def test_combined_changes(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/guide.md\n",
                       staged_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result
        assert "changelog" in result


class TestMainExtended:
    """Extended main() CLI mode tests for coverage boost."""

    def _mock_run_one_pass(self, short_name, script_path, tool_args,
                           project_root):
        return (short_name, "pass", 0.1, "ok", "output text")

    def _mock_run_one_fail(self, short_name, script_path, tool_args,
                           project_root):
        return (short_name, "fail", 0.2, "error detail", "failure output")

    def test_parallel_json(self, monkeypatch, capsys, cli_argv):
        """--parallel --json mode."""
        cli_argv('validate_all', '--parallel', '--json', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["mode"] == "parallel"

    def test_parallel_text(self, monkeypatch, capsys, cli_argv):
        """--parallel text mode."""
        cli_argv('validate_all', '--parallel', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "PARALLEL" in out

    def test_parallel_verbose(self, monkeypatch, capsys, cli_argv):
        """--parallel --verbose shows full output."""
        cli_argv('validate_all', '--parallel', '--verbose', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "output text" in out or "VERSIONS" in out

    def test_baseline_mode(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--baseline saves JSON baseline file."""
        bf = tmp_path / ".validation-baseline.json"
        monkeypatch.setattr(va, "BASELINE_FILE", bf)
        cli_argv('validate_all', '--baseline', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert bf.exists()
        data = json.loads(bf.read_text(encoding="utf-8"))
        assert "passed" in data

    def test_compare_mode(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--compare against baseline."""
        bf = tmp_path / ".validation-baseline.json"
        baseline = {
            "results": {"versions": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)
        cli_argv('validate_all', '--compare', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0

    def test_profile_mode(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--profile appends timing to CSV."""
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        cli_argv('validate_all', '--profile', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert csv_file.exists()
        content = csv_file.read_text(encoding="utf-8")
        assert "timestamp" in content
        assert "versions" in content

    def test_profile_appends(self, monkeypatch, capsys, tmp_path, cli_argv):
        """--profile appends (not overwrites) on second run."""
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)

        for _ in range(2):
            cli_argv('validate_all', '--profile', '--only', 'versions')
            monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
            with pytest.raises(SystemExit):
                va.main()

        lines = csv_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows

    def test_notify_pass(self, monkeypatch, capsys, cli_argv):
        """--notify on successful run."""
        calls = []
        monkeypatch.setattr(va, "_send_notification",
                            lambda t, m: calls.append((t, m)))
        cli_argv('validate_all', '--notify', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert len(calls) == 1
        assert "Passed" in calls[0][0]

    def test_notify_fail(self, monkeypatch, capsys, cli_argv):
        """--notify on failed run."""
        calls = []
        monkeypatch.setattr(va, "_send_notification",
                            lambda t, m: calls.append((t, m)))
        cli_argv('validate_all', '--notify', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_fail)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        assert len(calls) == 1
        assert "Failed" in calls[0][0]

    def test_fix_mode(self, monkeypatch, capsys, cli_argv):
        """--fix auto-fixes failed checks."""
        fix_calls = []

        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        def mock_subprocess_run(cmd, **kwargs):
            fix_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Fixed something"
            return result

        monkeypatch.setattr(va, "_run_one", mock_run_one)
        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

        # Find a tool that's in FIX_COMMANDS
        fix_name = next(iter(FIX_COMMANDS.keys()))
        cli_argv("validate_all", "--fix", "--only", fix_name)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Auto-fixing" in out

    def test_fix_no_auto_fix_available(self, monkeypatch, capsys, cli_argv):
        """--fix with a tool that has no auto-fix."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        monkeypatch.setattr(va, "_run_one", mock_run_one)

        # Find a tool NOT in FIX_COMMANDS
        tool_names = {t[0] for t in TOOLS}
        no_fix = next(n for n in tool_names if n not in FIX_COMMANDS)
        cli_argv("validate_all", "--fix", "--only", no_fix)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "no auto-fix" in out

    def test_smart_mode(self, monkeypatch, capsys, cli_argv):
        """--smart mode derives checks from git diff."""
        def mock_smart(project_root):
            return ["versions"]
        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        cli_argv('validate_all', '--smart')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Smart mode" in out

    def test_smart_mode_none(self, monkeypatch, capsys, cli_argv):
        """--smart with None (git unavailable) runs all."""
        def mock_smart(project_root):
            return None
        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        cli_argv('validate_all', '--smart', '--only', 'versions')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0

    def test_diff_report_mode(self, monkeypatch, capsys, cli_argv):
        """--diff-report shows diff output."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        monkeypatch.setattr(va, "_run_one", mock_run_one)

        def mock_gen_diff(failed_checks, tools_dir, project_root):
            return "=== DIFF REPORT ===\nversions: diff output"

        monkeypatch.setattr(va, "_generate_diff_report", mock_gen_diff)

        fix_name = next(iter(FIX_COMMANDS.keys()))
        cli_argv("validate_all", "--diff-report", "--only", fix_name)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "DIFF REPORT" in out

    def test_all_skipped_text_output(self, monkeypatch, capsys, cli_argv):
        """All tools skipped shows appropriate message."""
        cli_argv('validate_all', '--only', 'nonexistent_check')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "0" in out

    def test_fix_error_handling(self, monkeypatch, capsys, cli_argv):
        """--fix handles fix command errors."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        def mock_subprocess_run(cmd, **kwargs):
            raise OSError("Command not found")

        monkeypatch.setattr(va, "_run_one", mock_run_one)
        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

        fix_name = next(iter(FIX_COMMANDS.keys()))
        cli_argv("validate_all", "--fix", "--only", fix_name)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "fix error" in out or "Auto-fixing" in out
