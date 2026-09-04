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
 10. mermaid / links 兩列註冊參數的 pin（#1702）

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

    def test_failure_always_carries_a_reason(self, tmp_path):
        """A blank first line must not produce a red with no text at all.

        Which line a failure SHOULD quote is #1697's question; this pins only
        that the answer is never empty.
        """
        script = tmp_path / "blank_first_line.py"
        script.write_text(
            "import sys\nprint()\nprint('the real reason')\nsys.exit(1)\n",
            encoding="utf-8")
        _name, status, _elapsed, detail, _output = _run_one(
            "blank_first", str(script), [], str(tmp_path))
        assert status == "fail"
        assert detail.strip(), (
            "a failing check printed no reason at all; the operator gets a "
            "red tick and nothing else")

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
# main() --list 模式
# ============================================================

class TestListMode:
    """`--list` 的完整性。

    ⛔ 這個 class 原本也叫 `TestMainCLI`，與本檔下方（`_generate_diff_report`
    之後）那個同名 class 撞名。後定義的 binding 會覆蓋前一個，所以底下兩支
    測試在 `origin/main` 上 `--collect-only` **一次都沒有被 collect**。
    改名而不動測試內容（#1620）。

    ⚠️ 這件事沒有守衛：本 repo 未採用**通用 Python linter**，所以 F811 這類
    重定義檢查不存在。`.pre-commit-config.yaml` 唯一提到 `pyflakes` 的地方，
    是 actionlint 把該整合**釘成關閉**。⚠️ shellcheck **另有獨立且啟用中的
    hook**——它是 shell linter，不在這個軸上（⛔ 本段前一版把 shellcheck 寫進
    「唯一提到那類工具」這個全稱句裡，因而為假：那是拿掉一個會腐爛的數字之後
    留下的全稱句，由盲審抓出）。重新撞名會再次靜默：這是揭露，不是保護——
    它的靜默失效不會讓 #1620 的缺陷回來，只會讓 `--list` 的完整性重新失去見證。

    這一支之所以承重：#1620 新增的失敗訊息把 operator 導向 `--list`，
    而 `test_list_mode_shows_all_tools` 是唯一斷言 `--list` 印出**每一個**
    註冊名稱的東西。
    """

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
    """--diff-report 功能測試。

    The "fix runs" cases stub ``_unstaged_tracked_files`` clean so they test
    diff generation, not #1706's refusal; the refusal has its own cases below.
    """

    @staticmethod
    def _pretend_clean(monkeypatch):
        monkeypatch.setattr(va, "_unstaged_tracked_files", lambda root: [])

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

        self._pretend_clean(monkeypatch)
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

        self._pretend_clean(monkeypatch)
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

        self._pretend_clean(monkeypatch)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "no diff produced" in result

    # -------- #1706: the restore is `git checkout .` at the repo root -------

    def test_refuses_while_a_tracked_file_has_unstaged_changes(
            self, tmp_path, monkeypatch):
        """Nothing may run: the restore would take the operator's edits too."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            return mock

        # ⛔ Not a real tracked path: verify_diff.py builds its change map from
        # string literals in test files, so naming a real doc here would wire a
        # permanent phantom dependency between that doc and this module.
        monkeypatch.setattr(
            va, "_unstaged_tracked_files",
            lambda root: ["zz-not-a-real-path/edited-by-the-operator.md"])
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "Refusing to run" in result
        assert "zz-not-a-real-path/edited-by-the-operator.md" in result
        assert calls == [], (
            f"the refusal must happen before anything is spawned, got {calls}")

    def test_refuses_when_git_cannot_answer(self, tmp_path, monkeypatch):
        """A probe that did not run is not evidence of a clean tree."""
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = ""
            return mock

        monkeypatch.setattr(va, "_unstaged_tracked_files", lambda root: None)
        monkeypatch.setattr(subprocess, "run", mock_run)
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "Refusing to run" in result
        assert "could not be read" in result
        assert calls == []

    def test_refusal_names_a_way_back_to_green(self, tmp_path, monkeypatch):
        """The route must be `git add -u`, and stash must be marked as not
        equivalent: stashing the edit often clears the very failure that would
        have produced this report, so the operator gets silence instead.
        """
        monkeypatch.setattr(va, "_unstaged_tracked_files", lambda root: ["a.md"])
        result = va._generate_diff_report(
            {"versions": "fail"}, tmp_path, tmp_path)
        assert "git add -u" in result
        assert "stash" in result and "NOT equivalent" in result


class TestUnstagedTrackedFiles:
    """#1706's predicate, against a real repo — it decides who gets refused.

    The set has to be exactly what ``git checkout .`` overwrites. Too wide
    (untracked files, staged content) and the refusal fires on every worktree
    that has scratch files in it, which is the cheapest possible reason for
    someone to delete the guard.
    """

    @staticmethod
    def _repo(tmp_path):
        run = ["git", "-c", "core.filemode=false", "-c", "user.email=t@e.st",
               "-c", "user.name=t"]
        subprocess.run(run + ["init", "-q", str(tmp_path)],
                       check=True, timeout=30)
        (tmp_path / "tracked.md").write_text("one\n", encoding="utf-8")
        subprocess.run(run + ["-C", str(tmp_path), "add", "tracked.md"],
                       check=True, timeout=30)
        subprocess.run(run + ["-C", str(tmp_path), "commit", "-qm", "init"],
                       check=True, timeout=30)
        return tmp_path

    def test_clean_repo_reports_nothing(self, tmp_path):
        assert va._unstaged_tracked_files(self._repo(tmp_path)) == []

    def test_modified_tracked_file_is_reported(self, tmp_path):
        repo = self._repo(tmp_path)
        (repo / "tracked.md").write_text("two\n", encoding="utf-8")
        assert va._unstaged_tracked_files(repo) == ["tracked.md"]

    def test_untracked_file_is_not_reported(self, tmp_path):
        """`git checkout .` leaves untracked files alone, so they must pass."""
        repo = self._repo(tmp_path)
        (repo / "scratch.md").write_text("draft\n", encoding="utf-8")
        assert va._unstaged_tracked_files(repo) == []

    def test_staged_change_is_not_reported(self, tmp_path):
        """The restore comes from the index, so staged content survives it."""
        repo = self._repo(tmp_path)
        (repo / "tracked.md").write_text("staged\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "tracked.md"],
                       check=True, timeout=30)
        assert va._unstaged_tracked_files(repo) == []

    def test_non_ascii_name_is_reported_verbatim(self, tmp_path):
        """Without `-z` git C-quotes it, and naming the file is the whole
        point of the refusal. Every other case in this class stays green
        when `-z` is dropped, so this is the only one holding that flag.
        """
        repo = self._repo(tmp_path)
        name = "中文檔案.md"
        (repo / name).write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", name],
                       check=True, timeout=30)
        subprocess.run(["git", "-c", "user.email=t@e.st", "-c", "user.name=t",
                        "-C", str(repo), "commit", "-qm", "cjk"],
                       check=True, timeout=30)
        (repo / name).write_text("two\n", encoding="utf-8")
        assert va._unstaged_tracked_files(repo) == [name]

    def test_outside_a_repo_returns_none(self, tmp_path):
        """None, not [] — 'could not measure' must differ from 'measured OK'."""
        assert va._unstaged_tracked_files(tmp_path) is None


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
        """--skip 跳過指定 check。

        ⛔ 本測試原本只斷言 `len(out) > 0`——由建構方式保證為真，任何實作
        都過得了，而它是本檔唯一同時給 `--only` 與 `--skip` 的既有測試，
        也就是 #1620 改掉的那個語意名義上的覆蓋者。改為斷言**實際執行了
        什麼**（盲審抓出）。
        """
        calls = []

        def recording(short_name, script_path, tool_args, project_root):
            calls.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")

        cli_argv('validate_all', '--skip', 'versions,links', '--only', 'freshness')
        monkeypatch.setattr(va, "_run_one", recording)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 0
        assert calls == ["freshness"], (
            "--only picks freshness and --skip subtracts two names that were "
            "never selected, so exactly freshness must run")
        assert len(capsys.readouterr().out) > 0

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
        """--smart with None (git unavailable) runs all.

        ⛔ The first version passed `--only versions` alongside `--smart`,
        and `if args.smart and not only_set` short-circuits on that, so the
        mock was never called and the docstring described a path the test did
        not reach. The counter below is what makes the claim checkable
        (#1620).
        """
        calls = []
        ran = []

        def mock_smart(project_root):
            calls.append(project_root)
            return None

        def rec(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")

        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", rec)
        cli_argv('validate_all', '--smart')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert len(calls) == 1, "the detector was never consulted"
        assert len(ran) == len(va.TOOLS), (
            "git unavailable must run everything; ran %d of %d"
            % (len(ran), len(va.TOOLS)))

    @pytest.mark.parametrize("detected", [None, [], ["versions", "links"]])
    def test_smart_mode_announces_what_it_will_actually_run(
            self, monkeypatch, capsys, cli_argv, detected):
        """⛔ The announced count must survive `--skip`.

        The first version of this line derived the number from the smart
        selection alone, so `--smart --skip a,b,c` announced 33 and ran 30 --
        a fresh copy of the one-run-two-answers shape #1620 exists to remove,
        introduced by the same change that removed the original. Deriving it
        from the same filter the run uses is what keeps the two in step.
        """
        ran = []

        def rec(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")

        monkeypatch.setattr(va, "_smart_detect", lambda project_root: detected)
        monkeypatch.setattr(va, "_run_one", rec)
        cli_argv('validate_all', '--smart', '--skip', 'versions')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        raw = capsys.readouterr().out
        out = " ".join(raw.split())
        assert "versions" not in ran, "--skip must still subtract"
        # the announced number, whatever branch printed it, is the one that ran
        import re as _re
        nums = [int(x) for x in _re.findall(r"running (?:all )?(\d+)", out)]
        if detected is None:
            # git unavailable: the tool announces nothing at all, so
            # there is no number to contradict. Pin that instead.
            assert not nums, "no selection was made; %s" % out
            assert len(ran) == len(va.TOOLS) - 1, len(ran)
        else:
            assert nums, out
            assert nums[0] == len(ran), (
                "announced %r, ran %d (%s)" % (nums, len(ran), out))
            # ⛔ The names too, not just the count: reverting the list half
            # to `sorted(only_set)` while leaving the count derived left the
            # whole suite green and printed a check that does not run.
            line = next((l for l in raw.splitlines()
                         if l.startswith("Smart mode:")), "")
            if "git diff: " in line:
                announced = [x.strip() for x
                             in line.split("git diff: ", 1)[1].split(",")]
                assert sorted(announced) == sorted(ran), (
                    "announced %r, ran %r" % (announced, sorted(ran)))

    def test_smart_mode_with_an_empty_diff_does_not_claim_zero(
            self, monkeypatch, capsys, cli_argv):
        """⛔ An empty selection means NO RESTRICTION, so the run does
        everything. The tool used to print `running 0 check(s)` and then run
        all of them -- the same one-run-two-answers shape #1620 removed from
        `--only X --skip X`, measured on a clean worktree.

        This pins the report, not the behaviour: the fall-through is
        deliberately unchanged.
        """
        ran = []

        def rec(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")

        monkeypatch.setattr(va, "_smart_detect", lambda project_root: [])
        monkeypatch.setattr(va, "_run_one", rec)
        cli_argv('validate_all', '--smart')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = " ".join(capsys.readouterr().out.split())
        assert len(ran) == len(va.TOOLS), "an empty selection restricts nothing"
        assert "running 0 check" not in out, (
            "it announced 0 checks and then ran %d" % len(ran))
        assert "selected no checks" in out and str(len(va.TOOLS)) in out, out

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
        """`total == 0` prints the all-skipped summary, reached LEGALLY.

        ⛔ This test used to reach that branch with `--only nonexistent_check`
        and assert `code == 0`, i.e. it pinned #1620's defect as if it were
        the intended contract: a name nothing answers to was dropped silently
        and the run reported success. The docstring said only what the code
        did, so six reviewers read it as deliberate. The *branch* is worth
        covering; the invocation that reached it was not.

        Skipping every registered name is an explicit request to run nothing,
        so exit 0 is correct here and stays asserted. The name list is derived
        from TOOLS so it cannot rot into a partial skip that stops reaching
        this branch.
        """
        every_name = ",".join(n for n, _, _, _ in TOOLS)
        cli_argv('validate_all', '--skip', every_name)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "All tools skipped" in out

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


class TestTheGateIsStillSelected:
    """`cli_default_drift` must still be named in an `--only` list in both
    automatic callers. That is #1492's axis, and #1620's runtime guard does
    not cover it: a registered check nobody selects is never run and nothing
    says so.

    ⚠️ GREEN MEANS EXACTLY ONE THING: the name appears in an `--only` list
    in that file. It does NOT mean the check runs. Measured during #1620, all
    of these defeat it: a `--skip <same name>` beside the `--only`; `make
    lint-docs ARGS="--only x"` (argparse takes the last `--only`); a decoy
    mention elsewhere in the same file after the real recipe dropped it; an
    indented line-continuation truncating the list. Answering the real
    question means knowing WHICH invocation CI runs. An attempt to build that
    here was measured net-negative and reverted, and the apparatus that grew
    around it -- a three-state scanner, 26 shape fixtures and three tests
    whose job was to document its wrong answers -- was deleted with it: none
    of it went red when the fix under test was reverted, because none of it
    was testing the fix. #1492 owns the real mechanism.
    """

    _ONLY_RE = r"--only[= \t]+((?:[A-Za-z0-9_,]|,\s*\\\s*\n\s*|\\\s*\n\s*)+)"
    _CALLERS = ["Makefile", ".github/workflows/docs-ci.yaml"]
    _GATE = "cli_default_drift"

    @classmethod
    def _only_lists(cls, text):
        import re
        return [[n for n in re.sub(r"\\\s*\n\s*", "", m.group(1)).split(",") if n.strip()]
                for m in re.finditer(cls._ONLY_RE, text)]

    def _read(self, rel):
        from pathlib import Path
        return (Path(va.__file__).resolve().parents[2]
                / rel).read_text(encoding="utf-8")

    def _selects(self, text):
        return any(self._GATE in names for names in self._only_lists(text))

    @pytest.mark.parametrize("rel", _CALLERS)
    def test_the_gate_is_named_in_the_only_list(self, rel):
        """⛔ The control runs through the same predicate as the assertion,
        deliberately: `assert any(...)` over a scan is satisfied by anything
        that makes the scan find less, and a separate control test over the
        scan HELPER does not cover that -- measured, the short-circuited
        assertion survived while the helper was still exercised.
        """
        text = self._read(rel)
        assert self._only_lists(text), (
            f"no `--only` list could be read out of {rel} at all -- this "
            f"assertion would be vacuous, so it fails loudly instead of "
            f"passing quietly")
        assert self._selects(text), (
            f"no `--only` in {rel} names `{self._GATE}`. It stays in TOOLS, "
            f"every other assertion stays green, and nothing runs it (#1492)."
            f"{NL}Lists read: {self._only_lists(text)}")
        assert not self._selects(text.replace("," + self._GATE, "")), (
            f"control: removing `{self._GATE}` from {rel} left this green, so "
            f"the assertion above cannot be detecting it")


# ============================================================

def _parser_boolean_flags():
    """Every ``store_true`` flag the parser accepts, read from the source.

    ⛔ Derived, not listed. A flag added later joins the parametrize below on
    its own; a hand-written list would have to be remembered, and the reason
    that parametrize exists is that a hand-written witness — one mode — let
    every other mode bypass the guard undetected.

    Two flags are excluded on purpose, and both are asserted to still exist so
    a rename cannot empty the list quietly:

      ``--list``   returns BEFORE the guard, deliberately — it is the route the
                   failure message points at, so it has to keep working while
                   the invocation that produced that message is still on the
                   command line. Pinned by
                   ``test_list_still_works_alongside_a_bad_only``.
      ``--watch``  polls forever; it has its own test that replaces
                   ``_run_watch`` so a regression fails instead of hanging.
    """
    import ast
    from pathlib import Path
    src = Path(va.__file__).with_suffix(".py").read_text(encoding="utf-8")
    flags = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        if getattr(node.func, "attr", None) != "add_argument":
            continue
        if not any(kw.arg == "action"
                   and getattr(kw.value, "value", None) == "store_true"
                   for kw in node.keywords):
            continue
        for a in node.args:
            if isinstance(a, ast.Constant) and str(a.value).startswith("--"):
                flags.append(a.value)
    assert flags, "no store_true flags parsed; the parametrize below is vacuous"
    excluded = {"--list", "--watch"}
    missing = sorted(excluded - set(flags))
    assert not missing, (
        f"the documented exclusions {missing} are no longer parser flags; the "
        f"exclusion list has gone stale and is now hiding a real mode")
    return sorted(set(flags) - excluded)


_BOOL_FLAGS = _parser_boolean_flags()


class TestUnknownCheckNames:
    """A requested check name nothing answers to is EXIT_CALLER_ERROR (#1620).

    Measured on `cb93a9ff` (this branch's parent) before this change,
    each row alongside a control that had to behave differently and did:

    ======================================  ===  ==========================
    invocation                               rc   what the operator saw
    ======================================  ===  ==========================
    [control] --only versions                 0   1/1 passed, 32 skipped
    --only cli_default_drift_typo             0   Result: All tools skipped
    --only versions,cli_default_drift_typo    0   1/1 passed, 32 skipped
    --skip nonexistent_check --only versions  0   (no mention of it at all)
    --only versions --skip versions           0   both a skipped row AND a
                                                  passed row, for `versions`
    ======================================  ===  ==========================

    Row 3 is the one that matters. Both automatic callers pass a long list of
    names to `--only`, and this runner is the required status check
    `Drift Detection (validate_all.py)` — so one misspelled name reported a
    clean pass, at rc 0, with a real planted drift sitting unlooked-at.
    ⚠️ How long those lists are is deliberately not written down here: it is
    a number someone would have to come back and edit, and what actually has
    to hold about them is asserted instead, by
    `TestTheGateIsStillSelected`.

    Scope, stated so a green run is not read for more than it earns:

      COVERED      the NAME axis — a `--only` / `--skip` value that resolves
                   to no registered check.
      NOT COVERED  the sibling FLAG axis (a flag supplied and silently
                   ignored). ⛔ Named, never counted: the first draft of this
                   paragraph gave a count, and a blind review immediately
                   found an equal number it had not reached. The two families
                   are —
                     * SELECTION: one flag quietly wins over another (e.g.
                       `--smart` under `--only`, via `if args.smart and not
                       only_set`).
                     * EARLY RETURN: a branch exits before later flags are
                       read (the `--watch` dispatch; `--ci`'s exit inside the
                       run loop, which is ahead of the summary and of every
                       flag handled after it).
                   The per-flag measurements live in #1695, which is a
                   snapshot by construction — unlike this docstring, which
                   someone would otherwise have to maintain.
    """

    # ⛔ A second, INDEPENDENT literal on purpose — not `va.EXIT_CALLER_ERROR`.
    # Binding it to the constant the runner exits with would make the
    # assertion true by construction: the mutation `sys.exit(1)` in place of
    # `sys.exit(EXIT_CALLER_ERROR)` is killed here only because the two sides
    # are written down separately. This is a contract value (`_lib_exitcodes`,
    # dev-rules #13), not a measurement — nobody comes back to edit it.
    _CALLER_ERROR = 2

    # Chosen for the shapes a membership test gets wrong, not one obvious typo:
    #   cli_default_drift_typo — the measured CI-list misspelling
    #   version                — a PREFIX of a real name; an accept test built
    #                            on substring matching would take it
    #   VERSIONS               — case variant
    #   zz_not_a_check         — plainly absent
    _ABSENT = ("cli_default_drift_typo", "version", "VERSIONS",
               "zz_not_a_check")

    @staticmethod
    def _known():
        return {name for name, _, _, _ in TOOLS}

    @staticmethod
    def _recording_run_one(calls):
        def run_one(short_name, script_path, tool_args, project_root):
            calls.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")
        return run_one

    # ---- the probes themselves ------------------------------------------

    def test_the_probe_names_really_are_absent(self):
        """Control for every assertion below: if one of these ever became a
        real check name, its test would pass for the wrong reason."""
        assert self._ABSENT, (
            "the probe tuple is empty; every parametrize below collapses to "
            "zero cases and this control passes vacuously")
        assert not (set(self._ABSENT) & self._known())

    # ---- the accepted set is exactly the registry, both directions ------

    def test_every_registered_name_is_accepted(self):
        """Pins the ACCEPT set rather than a list of rejected spellings.

        A reject-list is structurally blind to "one more thing got accepted".
        This direction is what goes red if the guard's source is narrowed —
        e.g. derived from FIX_COMMANDS, a strict subset, instead of TOOLS.
        """
        known = self._known()
        assert va._unknown_check_names(known, set()) == []
        assert va._unknown_check_names(set(), known) == []

    @pytest.mark.parametrize("absent", _ABSENT)
    def test_nothing_outside_the_registry_is_accepted(self, absent):
        assert va._unknown_check_names({absent}, set()) == \
            [("--only", [absent])]
        assert va._unknown_check_names(set(), {absent}) == \
            [("--skip", [absent])]

    def test_both_flags_are_attributed_separately(self):
        """The operator has to be told WHICH flag to go and look at."""
        assert va._unknown_check_names({"zz_a", "versions"}, {"zz_b"}) == \
            [("--only", ["zz_a"]), ("--skip", ["zz_b"])]

    # ---- the call site (the function above proves nothing on its own) ----

    @pytest.mark.parametrize("mode", [[]] + [[f] for f in _BOOL_FLAGS],
                             ids=["bare"] + [f.lstrip("-") for f in _BOOL_FLAGS])
    def test_main_rejects_an_unknown_only_name(self, monkeypatch, capsys,
                                               cli_argv, mode, tmp_path):
        """⛔ Parametrised over MODES, not just the bare call.

        The guard sits ahead of every mode dispatch, and the source says so —
        but the first cut witnessed that for `--watch` alone. Measured then:
        making the guard conditional (`if problems and not args.smart:`, and
        the same for `--fix` / `--parallel` / `--json` / `--ci`) left the
        whole suite green, every time. A prose claim about all modes, with an
        assertion about one. This turns the claim into the assertion.
        """
        monkeypatch.setattr(va, "BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(va, "PROFILE_CSV", tmp_path / "profile.csv")
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', *mode,
                 '--only', 'versions,cli_default_drift_typo')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == self._CALLER_ERROR, (
            f"{mode or ['(no mode flag)']} must not carry the guard past the "
            f"unknown name")
        assert calls == [], (
            "a rejected invocation must not run anything — reporting on a "
            "partial run is the failure mode this replaces")
        err = capsys.readouterr().err
        assert "cli_default_drift_typo" in err, \
            "the message has to name the offending value"
        assert "--list" in err, \
            "the message has to point at a route to the right spelling"
        assert "--only" in err.splitlines()[0], (
            "the message has to name the flag the operator typed. Measured: "
            "swapping the two arguments at the call site made it say `--skip` "
            "for an `--only` typo, and nothing went red — while both automatic "
            "callers pass only `--only`, so CI would have pointed at a flag "
            "that is not in the command")

    def test_the_mode_list_is_the_parsers_own_set(self):
        """⛔ Anti-vacuity for the parametrize above, from an INDEPENDENT source.

        Measured: making `_parser_boolean_flags()` return `["--verbose"]` left
        the whole suite green — the parametrize just ran fewer cases, which is
        what a quantifier with no floor looks like. A count would rot with the
        next flag, and a floor derived the same way (re-reading the AST) would
        be a parallel rewrite of the thing it guards. So compare against
        argparse's OWN usage rendering, produced by the parser object rather
        than by reading its source: `[--ci]` is boolean, `[--skip SKIP]` is
        not, and the two exclusions have to be spelled out here too.
        """
        import re as _re
        r = subprocess.run(
            [sys.executable, va.__file__, "--help"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60)
        assert r.returncode == 0, r.stderr[:300]
        usage = r.stdout.split("\n\n", 1)[0]
        rendered = set(_re.findall(r"\[(--[A-Za-z0-9-]+)\]", usage))
        assert rendered, (
            "parsed no boolean flags out of argparse's usage line; this floor "
            "is measuring nothing")
        assert set(_BOOL_FLAGS) == rendered - {"--list", "--watch"}, (
            f"the derived mode list and argparse's own usage disagree: "
            f"derived-only={sorted(set(_BOOL_FLAGS) - rendered)}, "
            f"usage-only={sorted(rendered - set(_BOOL_FLAGS) - {'--list', '--watch'})}")

    @pytest.mark.parametrize("typo,want", [
        ("VERSIONS", "versions"),
        ("Cli_Default_Drift", "cli_default_drift"),
        ("TOOL_MAP", "tool_map"),
    ])
    def test_a_case_variant_still_gets_the_safe_remedy(self, typo, want):
        """⛔ difflib is case-sensitive, so an all-caps typo scored under the
        cutoff and the message offered NO name at all -- leaving `--list` as
        the only way out, which is the expensive one.

        The design of this whole message is "the safe remedy is also the
        cheapest, and it is on line 2". That guarantee was silently absent for
        this input class, and `VERSIONS` was already one of the probes here:
        the tests only asserted it was REJECTED, never that the rejection was
        usable (#1620).
        """
        msg = va._unknown_check_names_message([("--only", [typo])])
        assert "Did you mean" in msg, (
            "no suggestion at all for %r; the operator's only route is --list"
            % typo)
        assert want in msg, (typo, msg)


    @pytest.mark.parametrize("extra", [
        [], ["--only", "versions"], ["--smart"], ["--ci"],
        ["--watch", "--only", "versions"], ["--parallel"],
    ], ids=["bare", "only", "smart", "ci", "watch-only", "parallel"])
    def test_the_message_is_the_same_in_every_mode(
            self, capsys, cli_argv, extra):
        """⛔ The property three rounds of review died on.

        The message used to predict what the run would do, and the builder
        cannot see the mode: `--smart` fills the selection AFTER this guard,
        and `--watch` never reads `--only` at all. So every mode-dependent
        clause was false for some caller -- measured, in this order:
          * "deleting the name stops running it" -- false for a sole name,
            which runs everything instead;
          * "EVERY registered check runs" under `--skip` -- false whenever
            `--only` narrowed the run, and BOTH automatic callers pass one;
          * the `--only`-aware rewrite of that -- false under `--smart` (the
            selection is not filled yet) and under `--watch` (`--only` is
            read by nobody), and it told a watch user to edit a list that
            mode ignores.
        Each fix invented the next lie. What is pinned now is that there is
        nothing left to be mode-dependent: same bytes, every mode.
        """
        cli_argv('validate_all', *(extra + ['--skip', 'glosary']))
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert err == va._unknown_check_names_message(
            [("--skip", ["glosary"])]) + chr(10), err
        for predicted in ("EVERY registered check", "already narrowed",
                          "take it out of the --only list", "will then RUN"):
            assert predicted not in err, (
                "%r predicts the run; the builder cannot know the mode" %
                predicted)

    def test_each_flag_gets_its_own_consequence(self):
        """⛔ An early draft split the WARNING but left one shared body
        sentence -- `--only`'s failure shape, printed under `--skip` too --
        and the test of the day stayed green because it only read the
        warning. What is pinned is the property, not the sentences: each
        flag's block names that flag, and the two are not the same text.
        """
        only = va._unknown_check_names_message([("--only", ["zz_not_a_check"])])
        skip = va._unknown_check_names_message([("--skip", ["zz_not_a_check"])])
        assert "--only:" in only and "--skip:" not in only, only
        assert "--skip:" in skip and "--only:" not in skip, skip
        assert only != skip
        both = va._unknown_check_names_message(
            [("--only", ["tool_mapp"]), ("--skip", ["glossaryy"])])
        assert "--only:" in both and "--skip:" in both, (
            "when both flags are wrong the operator needs both consequences")


    def test_the_message_is_ascii_so_it_survives_a_cp950_console(self):
        """⚠️ This text goes to stderr, and `try_utf8_stdout` patches stdout
        only (by its own documented design). Measured before: on a cp950
        console the warning line degraded to `\\u26d4 … �X …` — the one line
        that carries the severity. Severity now rides on the word, not a glyph.
        """
        for problems in ([("--only", ["zz"])], [("--skip", ["zz"])],
                         [("--only", ["tool_mapp"])]):
            msg = va._unknown_check_names_message(problems)
            msg.encode("ascii")  # raises if a glyph sneaks back in

    def test_the_close_match_is_derived_not_canned(self):
        """A suggestion only appears when the registry actually has a
        neighbour; an unrelated name must not be handed a random check.

        ⚠️ The multi-name case is pinned too: measured, an earlier shape let
        the first name without a neighbour suppress the hint for all of them.
        """
        near = va._unknown_check_names_message([("--only", ["tool_mapp"])])
        assert "Did you mean (--only): tool_map" in near
        far = va._unknown_check_names_message([("--only", ["zz_not_a_check"])])
        assert "Did you mean" not in far
        mixed = va._unknown_check_names_message(
            [("--only", ["zz_not_a_check", "tool_mapp"])])
        assert "tool_map" in mixed, (
            "a name with no neighbour must not suppress the others' hints")

    def test_main_rejects_an_unknown_skip_name(self, monkeypatch, capsys,
                                               cli_argv):
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', '--skip', 'nonexistent_check',
                 '--only', 'versions')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == self._CALLER_ERROR
        assert calls == []
        assert "nonexistent_check" in capsys.readouterr().err

    def test_main_accepts_the_same_shape_spelled_correctly(self, monkeypatch,
                                                           capsys, cli_argv):
        """Control for the two above: same argv shape, real names.

        Without it, an implementation that rejects every `--only` whatsoever
        satisfies them both.

        ⚠️ The names are DERIVED from TOOLS. Hard-coded ones turned a correct,
        complete rename of a check into `assert 2 == 0` with the offending
        name only in captured stderr — measured by blind review, renaming
        `cli_default_drift` everywhere it is registered and selected.
        """
        picked = [n for n, _, _, _ in TOOLS][:2]
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', '--only', ",".join(picked))
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0, (
            f"{picked} are registered checks; this invocation must be accepted "
            f"(stderr: {capsys.readouterr().err.strip()[:200]!r})")
        assert sorted(calls) == sorted(picked)

    def test_list_still_works_alongside_a_bad_only(self, capsys, cli_argv):
        """⛔ `--list` returns before the guard, and that is deliberate.

        It is the route the failure message points at, so an operator holding
        a bad `--only` on the command line has to be able to append `--list`
        and get an answer. Pinned because the guard's placement ("before every
        mode dispatch") otherwise reads as if it should cover this one too.
        """
        cli_argv('validate_all', '--list', '--only', 'zz_not_a_check')
        assert va.main() is None
        out = capsys.readouterr().out
        for name, _, _, _ in TOOLS:
            assert name in out, f"--list must still show {name}"

    # ---- watch mode re-parses --skip itself, so the guard must precede it -

    def test_watch_mode_is_rejected_before_it_starts(self, monkeypatch,
                                                    cli_argv):
        """`_run_watch` builds its own skip_set (it is handed `args`, not our
        parsed sets), so an unknown name there would be dropped in its filter
        instead.

        `_run_watch` is replaced because the real one polls forever: a
        regression here would HANG rather than fail, and a hang reads as
        infrastructure trouble rather than as this assertion.
        """
        entered = []
        monkeypatch.setattr(va, "_run_watch",
                            lambda *a, **k: entered.append("started"))
        cli_argv('validate_all', '--watch', '--skip', 'zz_not_a_check')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == self._CALLER_ERROR
        assert entered == []

    def test_watch_mode_still_starts_for_a_registered_name(self, monkeypatch,
                                                           cli_argv):
        """Control: the guard must not have turned `--watch` into a dead flag.
        """
        entered = []
        monkeypatch.setattr(va, "_run_watch",
                            lambda *a, **k: entered.append("started"))
        cli_argv('validate_all', '--watch', '--skip', 'versions')
        assert va.main() is None
        assert entered == ["started"]

    # ---- symptom 3: --skip subtracts from --only ------------------------

    def test_skip_subtracts_from_only(self, monkeypatch, capsys, cli_argv):
        """Asserted on what EXECUTED, not on the report text.

        Before: `--skip` was ignored whenever `--only` was present, while the
        skipped-items loop printed `... skipped` for it anyway, so a single
        run reported both outcomes for one check. Pinning execution means a
        change to the report cannot satisfy this on its own.
        """
        keep, drop = [n for n, _, _, _ in TOOLS][:2]
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', '--only', f"{drop},{keep}", '--skip', drop)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0, (
            f"both {drop!r} and {keep!r} are registered; only the subtraction "
            f"should change what runs "
            f"(stderr: {capsys.readouterr().err.strip()[:200]!r})")
        assert calls == [keep], (
            f"--skip {drop} must remove it from the --only selection")

    def test_an_empty_intersection_runs_nothing_at_exit_zero(self, monkeypatch,
                                                             capsys,
                                                             cli_argv):
        """Every name existed; asking for an empty selection is a request to
        run nothing, not a bad invocation.

        This is the line between #1620 and a rule that would reject legal
        use: the predicate is "does a check answer to this name", never "did
        the selection come out empty".
        """
        import re
        one = TOOLS[0][0]
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', '--only', one, '--skip', one)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert calls == []
        out = capsys.readouterr().out
        assert "All tools skipped" in out
        # And the report no longer states two outcomes for one check: exactly
        # one row, naming that check, and its verdict is `skipped`.
        rows = [(name, tail.strip()) for _sym, name, tail
                in re.findall(r"^(\S+)\s+(\S+)\s+\.\.\.\s*(.*)$", out, re.M)]
        assert rows == [(one, "skipped")], (
            "before #1620 this printed a skipped row AND a passed row for the "
            "same check in one run")

    @pytest.mark.parametrize("value", ["", "   ", ",", " , ,"],
                             ids=["empty", "blank", "comma", "commas"])
    @pytest.mark.parametrize("flag", ["--only", "--skip"])
    def test_a_value_that_names_nothing_means_no_restriction(
            self, monkeypatch, capsys, cli_argv, flag, value):
        """A value that parses to zero names is NOT an unknown name.

        ⚠️ Pinned as intent rather than left silent (blind review). `--only
        "$CHECKS"` with an empty variable runs everything, which is the
        fail-safe direction — but "the tool did something other than what the
        argument said" with nothing written down is exactly #1620's shape, so
        it gets an assertion either way. The guard's predicate stays "does a
        check answer to this name"; an absent name is not an unknown one.
        """
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', flag, value)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0, (
            f"{flag} {value!r} names no check at all, so there is nothing for "
            f"the guard to reject "
            f"(stderr: {capsys.readouterr().err.strip()[:200]!r})")
        assert len(calls) == len(TOOLS), (
            f"{flag} {value!r} restricts nothing, so every registered check "
            f"runs")

    def test_whitespace_around_names_is_accepted(self, monkeypatch, capsys,
                                                 cli_argv):
        """⛔ `--only "a, b"` is a legal invocation and must stay legal.

        The runner strips each name before matching. Measured with that strip
        removed: `--only "links, mermaid"` became `exit 2` naming a check
        called `' mermaid'` — a caller error for a command that works. #1620's
        whole value rests on `exit 2` meaning the operator really did typo, so
        the strip is load-bearing and had no test.
        """
        first, second = [n for n, _, _, _ in TOOLS][:2]
        calls = []
        monkeypatch.setattr(va, "_run_one", self._recording_run_one(calls))
        cli_argv('validate_all', '--only', f" {first},  {second} ")
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0, (
            f"padded names must be accepted "
            f"(stderr: {capsys.readouterr().err.strip()[:200]!r})")
        assert sorted(calls) == sorted([first, second])

# ============================================================
# The two rows this change re-armed (#1702)
# ============================================================

class TestRearmedRows:
    """#1702. A PIN on two named rows, not a classifier.

    Dropping ``--ci`` makes a row incapable of failing while it still prints a
    tick; dropping a path narrows what it scans; repointing the script leaves
    args the new script may reject. All three are silent, so the row is pinned
    whole and a deliberate change has to come through here.

    ⚠️ NOT GUARDED, and this is the cost of a removal in this PR: nothing
    asserts that the OTHER rows can run or can fail. The withdrawn check and
    what it had measured are in the commit message; ``translation`` and
    ``freshness`` are still un-armed and tracked in #1735.
    """

    PINNED = {
        "mermaid": ("lint/validate_mermaid.py", ["docs/", "rule-packs/", "--ci"]),
        "links": ("lint/check_doc_links.py", ["--ci"]),
    }

    @pytest.mark.parametrize("name", sorted(PINNED))
    def test_row_script_and_args_are_pinned(self, name):
        """Script AND args — pinning only the args left a repointed row green."""
        rows = [r for r in TOOLS if r[0] == name]
        assert len(rows) == 1, f"{name} must be registered exactly once"
        assert (rows[0][1], rows[0][2]) == self.PINNED[name], (
            f"the {name} row changed. Dropping --ci makes the row incapable "
            f"of failing while still printing a tick; dropping a path "
            f"silently narrows what it scans; repointing the script leaves "
            f"args that the new script may reject. If the change is "
            f"deliberate, update this pin in the same commit.")
