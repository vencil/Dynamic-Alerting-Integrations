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

    @pytest.mark.parametrize("rule", ["---", "───────", "+---+---+", "...",
                                      "=== END ==="],
                             ids=["dashes", "box-drawing", "table-border",
                                  "dots", "banner"])
    def test_skips_rule_and_border_lines(self, rule):
        """⛔ Before #1697 only a `===`-prefixed line was skipped, so a tool
        that closes with a `---` rule or a `+---+` table border (measured:
        alerts' summary ends on `+---`) had THAT quoted as its detail — a
        row whose reason is a line of punctuation. The banner case is the
        pre-existing rule, kept as a control that widening did not drop it.
        """
        assert _extract_detail(f"the reason\n{rule}\n") == "the reason"

    def test_truncation_is_by_character_not_byte(self):
        """#1697 §3 worried the 80-cut "切在半個字上" for CJK. Measured: it
        does not — this is str slicing, so the cut is between code points
        and the result always re-encodes. Pinned so a future "optimise"
        to bytes (`.encode()[:80]`) — which WOULD split a 3-byte CJK char
        and yield a line that cannot be encoded back — goes red here.
        """
        line = "中" * 100
        got = _extract_detail(line)
        assert len(got) == 80, "cut by code points, not bytes"
        assert got == "中" * 80, "every character survives whole"
        got.encode("utf-8")  # a byte cut would leave a dangling lead byte
        # Control: the byte-sliced shape really is broken, so the assertion
        # above is discriminating rather than true of any slicing.
        broken = line.encode("utf-8")[:80]
        with pytest.raises(UnicodeDecodeError):
            broken.decode("utf-8")


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
    """_detect_changed_checks() 檔案變更偵測測試。

    Since #1704 the function returns a ``SmartSelection`` (checks /
    unmatched / changed) and no longer decides anything: the "run all"
    fallback that used to live in its last line is gone, because that
    fallback is what made an unmatched file and an explicit empty set
    indistinguishable to the caller.
    """

    def test_docs_change_triggers_doc_checks(self):
        """docs/ 目錄變更觸發文件相關 check。"""
        old = {"docs/guide.md": 1000}
        new = {"docs/guide.md": 2000}
        sel = _detect_changed_checks(old, new)
        assert "links" in sel.checks
        assert "versions" in sel.checks

    def test_rule_packs_change(self):
        """rule-packs/ 目錄變更觸發 rule pack 相關 check。"""
        old = {"rule-packs/mariadb.yaml": 1000}
        new = {"rule-packs/mariadb.yaml": 2000}
        sel = _detect_changed_checks(old, new)
        assert "alerts" in sel.checks
        assert "platform_data" in sel.checks

    def test_no_change_returns_empty(self):
        """無檔案變更：三個欄位皆空，`changed` 為空是 watch 迴圈的 continue 條件。"""
        snap = {"docs/guide.md": 1000}
        assert _detect_changed_checks(snap, snap) == va.SmartSelection([], [], [])

    def test_deleted_file_detected(self):
        """刪除的檔案也能偵測。"""
        old = {"docs/old.md": 1000}
        new = {}
        sel = _detect_changed_checks(old, new)
        assert sel.changed == ["docs/old.md"]
        assert len(sel.checks) > 0

    def test_new_file_detected(self):
        """新增的檔案也能偵測。"""
        old = {}
        new = {"docs/new.md": 1000}
        sel = _detect_changed_checks(old, new)
        assert sel.changed == ["docs/new.md"]
        assert len(sel.checks) > 0

    def test_scripts_tools_change(self):
        """scripts/tools/ 變更觸發 tool_map check。"""
        old = {"scripts/tools/ops/new_tool.py": 1000}
        new = {"scripts/tools/ops/new_tool.py": 2000}
        sel = _detect_changed_checks(old, new)
        assert "tool_map" in sel.checks

    def test_changelog_change(self):
        """CHANGELOG.md 變更觸發 changelog check。"""
        old = {"CHANGELOG.md": 1000}
        new = {"CHANGELOG.md": 2000}
        sel = _detect_changed_checks(old, new)
        assert "changelog" in sel.checks

    def test_docs_assets_triggers_platform_data(self):
        """docs/assets/ 變更觸發 platform_data 與 tool_consistency 檢查。"""
        old = {"docs/assets/data.json": 1000}
        new = {"docs/assets/data.json": 2000}
        sel = _detect_changed_checks(old, new)
        assert "platform_data" in sel.checks
        assert "tool_consistency" in sel.checks

    def test_result_is_sorted(self):
        """回傳結果按字母排序。"""
        old = {"docs/a.md": 1000, "rule-packs/b.yaml": 1000}
        new = {"docs/a.md": 2000, "rule-packs/b.yaml": 2000}
        sel = _detect_changed_checks(old, new)
        assert sel.checks == sorted(sel.checks)
        assert sel.changed == sorted(sel.changed)

    # ---- #1704: the three outcomes, on the pure function -----------------

    def test_unmatched_file_is_reported_not_turned_into_run_all(self):
        """#1704 outcome 1 (no information). Before, this function answered
        an unmatched file with the full TOOLS list -- the SAME value it
        would return for "every check is affected", so the watch loop could
        not say why it was re-running everything. Now the file is NAMED in
        `unmatched` and `checks` says what the matched files (none) said;
        the fail-safe "run all" is the caller's decision, via
        _selection_outcome.
        """
        sel = _detect_changed_checks({}, {"some_random_file.txt": 1000})
        assert sel.unmatched == ["some_random_file.txt"]
        assert sel.checks == []
        outcome, chosen = va._selection_outcome(sel)
        assert outcome == va.OUTCOME_UNKNOWN
        assert chosen == [n for n, _, _, _ in TOOLS], "fail-safe: every check"

    def test_precommit_config_alone_is_an_explicit_empty_set(self):
        """#1704 outcome 2 (explicit empty). `WATCH_TRIGGERS[".pre-commit-
        config.yaml"] = []` reads "affects no check", but the old
        `sorted(affected) if affected else <all>` made an empty union mean
        "run all 33" -- so the entry was dead configuration. It is matched
        (not in `unmatched`), contributes nothing, and the outcome is EMPTY:
        nothing to run.
        """
        sel = _detect_changed_checks({".pre-commit-config.yaml": 1},
                                     {".pre-commit-config.yaml": 2})
        assert sel.changed == [".pre-commit-config.yaml"]
        assert sel.unmatched == [], "it matched a trigger; the list is just empty"
        assert sel.checks == []
        outcome, chosen = va._selection_outcome(sel)
        assert outcome == va.OUTCOME_EMPTY
        assert chosen == []

    def test_precommit_config_plus_an_unmatched_file_is_unknown_not_empty(self):
        """Paired control for the two above: both have `checks == []`, so an
        implementation that classifies on `checks` alone would call this
        EMPTY and run nothing for a file whose impact nobody knows. The
        unmatched file has to win.
        """
        sel = _detect_changed_checks(
            {}, {".pre-commit-config.yaml": 1, "zz_unknown.cfg": 1})
        assert sel.checks == []
        assert sel.unmatched == ["zz_unknown.cfg"]
        outcome, _ = va._selection_outcome(sel)
        assert outcome == va.OUTCOME_UNKNOWN

    def test_a_matched_file_with_checks_is_selected(self):
        """#1704 outcome 3 (control): the ordinary case still yields exactly
        the union, not "all" and not "nothing"."""
        sel = _detect_changed_checks({}, {"CHANGELOG.md": 1})
        outcome, chosen = va._selection_outcome(sel)
        assert outcome == va.OUTCOME_SELECTED
        assert chosen == ["changelog"]

    def test_none_means_git_could_not_answer_and_is_unknown(self):
        """`_smart_detect` returns None when git cannot answer; that is "no
        information", the same outcome as an unmatched file, never "clean
        tree, run nothing"."""
        outcome, chosen = va._selection_outcome(None)
        assert outcome == va.OUTCOME_UNKNOWN
        assert chosen == [n for n, _, _, _ in TOOLS]
        assert "git could not be read" in va._unmatched_clause(None)


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

    # ---- #1703: a check that vanishes from the run set ----------------

    @staticmethod
    def _write_baseline(tmp_path, monkeypatch, results):
        bf = tmp_path / "baseline.json"
        bf.write_text(json.dumps({
            "results": results,
            "passed": sum(1 for r in results.values() if r["status"] == "pass"),
            "failed": sum(1 for r in results.values() if r["status"] != "pass"),
        }), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)

    def test_a_check_that_vanished_is_a_named_regression(
            self, capsys, tmp_path, monkeypatch):
        """#1703. `pass → N/A` fell through both branches (pass→fail,
        fail→pass), so `regressions` stayed empty and the tool printed
        `✅ No regressions detected.` directly under `3 pass → 2 pass` —
        the exact shape this family of incidents is reported in ("required
        check green, but one check never ran"), and --compare was the only
        detector. The line has to NAME the check and say it vanished, not
        that it failed: an operator running `--only` on purpose reads that
        and moves on; the one in the incident needs the name.
        """
        self._write_baseline(tmp_path, monkeypatch, {
            "links": {"status": "pass", "elapsed": 1.0},
            "versions": {"status": "pass", "elapsed": 1.0},
        })
        _compare_baseline({
            "results": {"links": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        })
        err = capsys.readouterr().err
        assert "Regressions" in err
        assert "versions" in err, "the vanished check must be named"
        line = next(ln for ln in err.splitlines() if "versions" in ln)
        assert "vanished" in line and "did not fail" in line, line
        assert "No regressions detected" not in err, (
            "the verdict said green while a check was missing from the run")

    def test_identical_sets_still_say_no_regressions(
            self, capsys, tmp_path, monkeypatch):
        """Control for the test above: an implementation that flags every
        comparison would also satisfy it. Same two checks on both sides,
        same statuses → the ✅ line is still printed.
        """
        results = {
            "links": {"status": "pass", "elapsed": 1.0},
            "versions": {"status": "pass", "elapsed": 1.0},
        }
        self._write_baseline(tmp_path, monkeypatch, results)
        _compare_baseline({"results": results, "passed": 2, "failed": 0})
        err = capsys.readouterr().err
        assert "No regressions detected" in err
        assert "Regressions" not in err
        assert "vanished" not in err

    def test_a_new_check_is_reported_but_not_as_a_regression(
            self, capsys, tmp_path, monkeypatch):
        """The reverse direction (#1703's last paragraph): `N/A → pass` used
        to be silent, and silence on one side is how the two sides of a
        comparison drift apart. It is reported under its own neutral
        heading — it is neither a regression nor an improvement — and the
        ✅ verdict is unaffected by it.
        """
        self._write_baseline(tmp_path, monkeypatch, {
            "links": {"status": "pass", "elapsed": 1.0},
        })
        _compare_baseline({
            "results": {"links": {"status": "pass", "elapsed": 1.0},
                        "versions": {"status": "pass", "elapsed": 1.0}},
            "passed": 2, "failed": 0,
        })
        err = capsys.readouterr().err
        assert "New checks" in err
        assert "versions" in err, "the new check must be named"
        assert "Regressions" not in err
        assert "No regressions detected" in err

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

    def test_an_empty_trigger_list_means_matched_and_affects_nothing(self):
        """#1704. The empty list is a meaningful value now, so pin what it
        means at the derivation both modes share: the file is MATCHED (not
        reported as unknown) and contributes no check. The entry itself is
        asserted to still be empty -- if someone adds checks to it, this
        pin is the reminder that the empty-list semantics need another
        witness.
        """
        assert WATCH_TRIGGERS[".pre-commit-config.yaml"] == []
        sel = va._select_checks([".pre-commit-config.yaml"])
        assert sel == va.SmartSelection([], [], [".pre-commit-config.yaml"])


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

    def test_failure_quotes_the_reason_not_the_opening_success_line(
            self, tmp_path):
        """#1697. A failing check used to quote its FIRST stdout line; the
        repo's tools print progress first and the reason last, so the row
        read `✗ tool_map ... (✅ Tool map (zh) is up to date.)` — symbol
        says fail, parenthesis says pass. Reproduced with the issue's own
        minimal tool, and on the real tool_map / doc_map rows in a scratch
        copy of the repo (OLD `✅ Tool map (zh) is up to date.` → NEW `❌
        docs/internal/tool-map.en.md is outdated …`).
        """
        script = tmp_path / "progress_then_error.py"
        script.write_text(
            "import sys\n"
            "print('OK: everything is fine')\n"
            "print('ERROR: something is broken')\n"
            "sys.exit(1)\n",
            encoding="utf-8")
        _name, status, _elapsed, detail, _output = _run_one(
            "progress_then_error", str(script), [], str(tmp_path))
        assert status == "fail"
        assert detail == "ERROR: something is broken", (
            f"the row quoted {detail!r}: the opening line, not the reason")
        assert "everything is fine" not in detail

    def test_failure_with_a_single_error_line_still_quotes_it(self,
                                                              tmp_path):
        """Control for the test above: with one line only, first == last,
        so an implementation that merely SKIPS the first line would print
        nothing here and fall through to `Exit code: 1`. The reason must
        still be the line the tool printed.
        """
        script = tmp_path / "single_line.py"
        script.write_text(
            "import sys\nprint('ERROR: the only thing said')\nsys.exit(1)\n",
            encoding="utf-8")
        _name, status, _elapsed, detail, _output = _run_one(
            "single_line", str(script), [], str(tmp_path))
        assert status == "fail"
        assert detail == "ERROR: the only thing said"

    def test_failure_with_no_stdout_falls_back_to_exit_code(self, tmp_path):
        """The `Exit code: N` fallback is kept on purpose: measured for
        #1697, 9 of 15 induced failures print their reason to stderr only
        (glossary, alerts, rule_packs, …), so an empty stdout is the common
        case, not a corner. Reading stderr is a separate change.
        """
        script = tmp_path / "stderr_only.py"
        script.write_text(
            "import sys\nprint('reason on stderr', file=sys.stderr)\n"
            "sys.exit(3)\n",
            encoding="utf-8")
        _name, status, _elapsed, detail, _output = _run_one(
            "stderr_only", str(script), [], str(tmp_path))
        assert status == "fail"
        assert detail == "Exit code: 3"

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

    def test_ci_stops_on_failure(self, monkeypatch, capsys, cli_argv):
        """--ci 模式遇到失敗時 exit 1 —— 而且是從結尾統一的 exit 出去的。

        #1695 家族二之前，這個 exit 1 是迴圈中間的 `sys.exit(1)`：rc 對、
        但 `Result:` 摘要與後面每個旗標都沒跑。這支原本只斷言 rc，所以在
        兩種實作下都綠；現在同時斷言摘要有印，把「rc 對」與「尾段有跑」
        分開見證（完整的五對 twin 在 TestCiStopFallsThroughToTheTail）。
        """
        cli_argv('validate_all', '--ci', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_fail)
        with pytest.raises(SystemExit) as exc_info:
            va.main()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Stopping after failure (versions)" in out
        assert "Result:" in out, "the summary was skipped by the mid-loop exit"

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
    """_smart_detect() git-diff based check selection.

    Since #1704 it returns a ``SmartSelection`` or None; "run all" is no
    longer a value it can produce (see TestDetectChangedChecks for why).
    """

    def _mock_git(self, monkeypatch, diff_files="", staged_files="",
                  untracked_files="", fail=False, rc=0):
        """Mock subprocess.run for git commands."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if fail:
                raise subprocess.TimeoutExpired(cmd, 30)
            result.returncode = rc
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

    def test_no_changes_returns_an_empty_selection(self, monkeypatch, tmp_path):
        """A clean tree is an EMPTY selection, distinct from None."""
        self._mock_git(monkeypatch)
        result = _smart_detect(tmp_path)
        assert result == va.SmartSelection([], [], [])

    def test_docs_change_triggers_doc_checks(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="docs/guide.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result.checks
        assert "versions" in result.checks

    def test_rule_packs_change(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="rule-packs/mariadb.yaml\n")
        result = _smart_detect(tmp_path)
        assert "alerts" in result.checks
        assert "platform_data" in result.checks

    def test_unknown_file_is_named_not_turned_into_run_all(self, monkeypatch,
                                                           tmp_path):
        """#1704. The old detector returned the full TOOLS list here --
        indistinguishable from a diff that really touched everything, and
        the reason (which file?) was lost before main() saw it."""
        self._mock_git(monkeypatch, diff_files="unknown_file.txt\n")
        result = _smart_detect(tmp_path)
        assert result.unmatched == ["unknown_file.txt"]
        assert result.checks == []

    def test_timeout_returns_none(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, fail=True)
        result = _smart_detect(tmp_path)
        assert result is None

    def test_a_git_command_that_fails_returns_none(self, monkeypatch, tmp_path):
        """#1704. A probe with a non-zero rc used to be skipped and `changed`
        stayed empty -- harmless when empty meant "run all", but now empty
        means "run nothing", so `git diff HEAD` failing (not a repo, unborn
        HEAD) would have read as a clean tree. Failure is "no information",
        i.e. None.
        """
        self._mock_git(monkeypatch, rc=128)
        assert _smart_detect(tmp_path) is None

    def test_staged_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, staged_files="scripts/tools/ops/new.py\n")
        result = _smart_detect(tmp_path)
        assert "tool_map" in result.checks

    def test_untracked_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, untracked_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "changelog" in result.checks

    def test_result_is_sorted(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/a.md\nrule-packs/b.yaml\n")
        result = _smart_detect(tmp_path)
        assert result.checks == sorted(result.checks)
        assert result.changed == sorted(result.changed)

    def test_combined_changes(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/guide.md\n",
                       staged_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result.checks
        assert "changelog" in result.checks


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

    # ---- #1705: header ↔ row column drift, and the mode column ----------

    def _derived_header(self):
        return ("timestamp,mode,wall_time," +
                ",".join(f"{n}_time,{n}_status" for n, _, _, _ in va.TOOLS))

    def test_profile_header_mismatch_rotates_instead_of_appending(
            self, monkeypatch, capsys, tmp_path, cli_argv):
        """#1705. The header was written once, on file creation, while every
        row expanded the CURRENT `TOOLS` — so registering a check made each
        later row two columns wider than the header with no warning, and
        one column index then meant different checks in early and late
        rows. A file whose header does not match the derived one must not
        be appended to: it is moved aside and a fresh file starts with the
        header the rows actually use.
        """
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        stale = "timestamp,mode,wall_time,old_check_time,old_check_status\n"
        csv_file.write_text(stale + "2026-01-01T00:00:00+00:00,sequential,1.00,0.5,pass\n",
                            encoding="utf-8")
        cli_argv('validate_all', '--profile', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        lines = csv_file.read_text(encoding="utf-8").split("\n")
        assert lines[0] == self._derived_header(), "the new file starts with the derived header"
        assert len(lines[1].split(",")) == len(lines[0].split(",")), (
            "row and header must have the same number of columns")
        assert "old_check" not in csv_file.read_text(encoding="utf-8")
        backups = sorted(tmp_path.glob(".validation-profile.csv.bak-*"))
        assert len(backups) == 1, "the stale file must be rotated aside, not lost"
        assert backups[0].read_text(encoding="utf-8").startswith(stale), (
            "the rotated file is the old one, byte for byte")
        assert "header no longer matches" in capsys.readouterr().err, (
            "the rotation has to be announced; silence is the defect")

    def test_profile_matching_header_is_appended_to(
            self, monkeypatch, capsys, tmp_path, cli_argv):
        """Control: a file whose header IS the derived one keeps growing —
        an implementation that rotates on every run would pass the test
        above and destroy the trend data the flag exists to collect.
        """
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        csv_file.write_text(self._derived_header() + "\n", encoding="utf-8")
        cli_argv('validate_all', '--profile', '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit):
            va.main()
        assert not list(tmp_path.glob(".validation-profile.csv.bak-*"))
        lines = csv_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2, "header + the appended row"
        assert "header no longer matches" not in capsys.readouterr().err

    def test_effective_mode_is_the_branch_taken_not_the_flag(self):
        """#1705 neighbour 2. `--ci --parallel` took the sequential branch
        (`args.parallel and not args.ci`) while the header, the JSON `mode`
        and the CSV `mode` column each read `args.parallel` and said
        `parallel`. The derivation is pinned here at the helper because the
        pair itself is now exit 2 at main() (#1695 family 1,
        TestConflictingFlags), so main() can no longer reach a CSV row for
        it; the wiring of the three labels to this helper is covered by the
        two end-to-end tests below.
        """
        import argparse
        assert va._effective_mode(argparse.Namespace(parallel=True, ci=True)) == "sequential"
        assert va._effective_mode(argparse.Namespace(parallel=True, ci=False)) == "parallel"
        assert va._effective_mode(argparse.Namespace(parallel=False, ci=True)) == "sequential"
        assert va._effective_mode(argparse.Namespace(parallel=False, ci=False)) == "sequential"

    @pytest.mark.parametrize("flags,want", [
        (["--parallel"], "parallel"),
        (["--ci"], "sequential"),
    ], ids=["parallel", "ci"])
    def test_profile_and_json_mode_come_from_effective_mode(
            self, monkeypatch, capsys, tmp_path, cli_argv, flags, want):
        """Wiring for the helper above: the CSV `mode` column and the JSON
        `mode` field must both carry the effective mode. Two rows so that
        a constant would fail one of them.
        """
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        cli_argv('validate_all', *flags, '--profile', '--json',
                 '--only', 'versions')
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert json.loads(capsys.readouterr().out)["mode"] == want
        row = csv_file.read_text(encoding="utf-8").strip().split("\n")[1]
        assert row.split(",")[1] == want

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
            return va.SmartSelection(["versions"], [], ["mkdocs.yml"])
        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        cli_argv('validate_all', '--smart')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Smart mode" in out

    def test_smart_mode_none(self, monkeypatch, capsys, cli_argv):
        """--smart with None (git unavailable) runs all -- and says why.

        ⛔ The first version passed `--only versions` alongside `--smart`,
        and `if args.smart and not only_set` short-circuits on that, so the
        mock was never called and the docstring described a path the test did
        not reach. The counter below is what makes the claim checkable
        (#1620). Since #1704 this is outcome 1 ("no information"): the tool
        used to fall through in silence, now the line names the cause.
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
        out = capsys.readouterr().out
        assert "git could not be read" in out and "impact unknown" in out, out

    @pytest.mark.parametrize("detected", [
        None,
        va.SmartSelection([], ["zz_unknown.cfg"], ["zz_unknown.cfg"]),
        va.SmartSelection(["links", "versions"], [], ["docs/x.md"]),
    ], ids=["git-unavailable", "unmatched-file", "selected"])
    def test_smart_mode_announces_what_it_will_actually_run(
            self, monkeypatch, capsys, cli_argv, detected):
        """⛔ The announced count must survive `--skip`.

        The first version of this line derived the number from the smart
        selection alone, so `--smart --skip a,b,c` announced 33 and ran 30 --
        a fresh copy of the one-run-two-answers shape #1620 exists to remove,
        introduced by the same change that removed the original. Deriving it
        from the same filter the run uses is what keeps the two in step.
        Since #1704 every branch that runs something announces a number (the
        None branch used to announce nothing), so all three are held to it;
        the branch that runs NOTHING has its own test below.
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
        assert nums, out
        assert nums[0] == len(ran), (
            "announced %r, ran %d (%s)" % (nums, len(ran), out))
        if detected is None or detected.unmatched:
            assert len(ran) == len(va.TOOLS) - 1, len(ran)
        else:
            # ⛔ The names too, not just the count: reverting the list half
            # to `sorted(only_set)` while leaving the count derived left the
            # whole suite green and printed a check that does not run.
            line = next((ln for ln in raw.splitlines()
                         if ln.startswith("Smart mode:")), "")
            assert "git diff: " in line, line
            announced = [x.strip() for x
                         in line.split("git diff: ", 1)[1].split(",")]
            assert sorted(announced) == sorted(ran), (
                "announced %r, ran %r" % (announced, sorted(ran)))

    def test_smart_mode_on_a_clean_tree_runs_nothing(
            self, monkeypatch, capsys, cli_argv):
        """#1704. An empty selection now means RUN NOTHING.

        ⛔ The previous version of this test pinned the opposite: "an empty
        selection restricts nothing", asserting all 33 ran while the line
        said `selected no checks, so nothing is restricted -- running 33`.
        That wording was #1620's STOPGAP -- it only corrected the printed
        number (`running 0 check(s)` followed by 33 rows) and left the
        fall-through alone, deferring the semantics to #1704 on purpose.
        The fall-through existed because the selection was poured into
        `only_set`, whose emptiness means "no restriction" for `--only`;
        so a clean tree, `--only ""` and "every check" were one value.
        #1704 decided: nothing to select is nothing to run -- that is what
        `smart` means, and no automatic caller passes `--smart` (measured:
        Makefile and docs-ci both pass `--only`; a repo-wide grep finds
        `--smart` only in this tool, this file and the CHANGELOG).
        """
        ran = []

        def rec(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")

        monkeypatch.setattr(va, "_smart_detect",
                            lambda project_root: va.SmartSelection([], [], []))
        monkeypatch.setattr(va, "_run_one", rec)
        cli_argv('validate_all', '--smart')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = " ".join(capsys.readouterr().out.split())
        assert ran == [], "an empty selection ran %d check(s)" % len(ran)
        assert "Smart mode: nothing to run" in out, out
        assert "no changes against HEAD" in out, out
        assert "Use --only" in out and "drop --smart" in out, out
        assert "All tools skipped" in out, "the un-run checks count as skipped"
        assert "running 0 check" not in out and "running 33" not in out, out

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


class TestSmartModeThreeOutcomes:
    """#1704: `--smart` keeps "no information" and "an empty set" apart,
    end to end through the real `_smart_detect` (git mocked, `_run_one`
    mocked). Measured on this branch's parent, all four rows below ran
    every registered check at rc 0, and only one of them had a reason to:

      clean tree                       ran 33, `selected no checks ... running 33`
      only .pre-commit-config.yaml     ran 33, same line (the [] entry was dead)
      one file outside every prefix    ran 33, NO line at all (silent)
      git unavailable                  ran 33, NO line at all (silent)

    Now: the first two run nothing and say why; the last two run
    everything and say why, naming the file / the cause. Outcome 3 (a
    non-empty selection) is the control and is unchanged. Outcome "git
    unavailable" is pinned in TestMainExtended.test_smart_mode_none.
    """

    @staticmethod
    def _git(monkeypatch, diff_files=""):
        """Only `git diff --name-only HEAD` reports files; the index and
        untracked probes are clean. `_run_one` is replaced separately, so
        this mock serves git and nothing else."""
        def mock_run(cmd, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = diff_files if ("diff" in cmd
                                      and "--cached" not in cmd) else ""
            return r
        monkeypatch.setattr(subprocess, "run", mock_run)

    @staticmethod
    def _recording(monkeypatch):
        ran = []

        def rec(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")
        monkeypatch.setattr(va, "_run_one", rec)
        return ran

    def _run(self, capsys, cli_argv, *argv):
        cli_argv('validate_all', '--smart', *argv)
        with pytest.raises(SystemExit) as exc:
            va.main()
        captured = capsys.readouterr()
        return exc.value.code, captured.out, captured.err

    def test_a_clean_tree_runs_nothing_and_says_so(
            self, monkeypatch, capsys, cli_argv):
        """(a) Explicit empty: nothing changed -> nothing runs, exit 0, and
        the line says why and how to run something anyway."""
        self._git(monkeypatch)
        ran = self._recording(monkeypatch)
        rc, out, _ = self._run(capsys, cli_argv)
        assert rc == 0
        assert ran == []
        line = next(ln for ln in out.splitlines() if ln.startswith("Smart mode:"))
        assert "nothing to run" in line and "no changes against HEAD" in line
        assert "Use --only to pick checks or drop --smart" in line
        line.encode("ascii")  # under --json the same line goes to stderr
        assert "Result: All tools skipped" in out

    def test_only_the_precommit_config_changed_runs_nothing(
            self, monkeypatch, capsys, cli_argv):
        """(b) Explicit empty via an empty trigger list: the file MATCHED
        `.pre-commit-config.yaml: []`, so its impact is known to be none.
        Before #1704 that entry was dead configuration -- the empty union
        fell through to "run all 33" exactly like an unknown file."""
        self._git(monkeypatch, ".pre-commit-config.yaml\n")
        ran = self._recording(monkeypatch)
        rc, out, _ = self._run(capsys, cli_argv)
        assert rc == 0
        assert ran == []
        line = next(ln for ln in out.splitlines() if ln.startswith("Smart mode:"))
        assert "nothing to run" in line, line
        assert ".pre-commit-config.yaml" in line and "affect no check" in line
        assert "Result: All tools skipped" in out

    def test_a_file_outside_every_prefix_runs_all_and_names_it(
            self, monkeypatch, capsys, cli_argv):
        """(c) No information: paired control for (b). Both have an empty
        union of check lists; the difference is whether the file matched a
        trigger at all. An unknown file's impact is unknown, so everything
        runs -- and the operator is told which file caused that, which the
        old silent fall-through never did."""
        self._git(monkeypatch, "zz_unknown.cfg\n")
        ran = self._recording(monkeypatch)
        rc, out, _ = self._run(capsys, cli_argv)
        assert rc == 0
        assert ran == [n for n, _, _, _ in TOOLS]
        line = next(ln for ln in out.splitlines() if ln.startswith("Smart mode:"))
        assert "zz_unknown.cfg" in line, "the unmatched file must be named"
        assert "match no known trigger" in line and "impact unknown" in line
        assert f"running all {len(TOOLS)} check(s)" in line, line

    def test_precommit_config_plus_an_unknown_file_runs_all(
            self, monkeypatch, capsys, cli_argv):
        """(b)+(c) together: the unknown file wins. An implementation that
        classified on "is the union empty" would run nothing here."""
        self._git(monkeypatch, ".pre-commit-config.yaml\nzz_unknown.cfg\n")
        ran = self._recording(monkeypatch)
        rc, out, _ = self._run(capsys, cli_argv)
        assert rc == 0
        assert len(ran) == len(TOOLS)
        assert "zz_unknown.cfg" in out and "nothing to run" not in out

    def test_skip_still_subtracts_from_a_non_empty_selection(
            self, monkeypatch, capsys, cli_argv):
        """(e) Outcome 3 control, with `--skip`: the selection is used and
        `--skip` subtracts from it (#1620), and the announced names are the
        ones that ran."""
        self._git(monkeypatch, "CHANGELOG.md\nmkdocs.yml\n")
        ran = self._recording(monkeypatch)
        rc, out, _ = self._run(capsys, cli_argv, '--skip', 'versions')
        assert rc == 0
        assert ran == ["changelog"], ran
        line = next(ln for ln in out.splitlines() if ln.startswith("Smart mode:"))
        assert line.rstrip().endswith("based on git diff: changelog"), line

    @pytest.mark.parametrize("diff_files,total", [
        ("", 0), ("CHANGELOG.md\n", 1),
    ], ids=["clean-tree", "selected"])
    def test_json_stdout_is_one_document_and_the_reason_goes_to_stderr(
            self, monkeypatch, capsys, cli_argv, diff_files, total):
        """Under `--json` the Smart-mode line moves to stderr so stdout stays
        a single JSON document (the same rule the --profile rotation notice
        follows); the reason is never dropped. On a clean tree the document
        carries total 0 and every check as skipped."""
        self._git(monkeypatch, diff_files)
        self._recording(monkeypatch)
        rc, out, err = self._run(capsys, cli_argv, '--json')
        assert rc == 0
        data = json.loads(out)
        assert data["total"] == total
        assert data["skipped"] == len(TOOLS) - total
        assert "Smart mode:" in err and "Smart mode:" not in out


class TestCiStopFallsThroughToTheTail:
    """#1695 family 2, the `--ci` half. The first failure used to
    `sys.exit(1)` INSIDE the sequential loop, ahead of the summary and of
    every flag handled after it. Measured on this branch's parent, with
    `_run_one` mocked to fail (the issue's own table; the right-hand column
    is the non-`--ci` twin, which is what every row here is paired with):

      --only versions --fix --ci           fix subprocess: 0 calls   (twin: 1)
      --only versions --profile --ci       CSV: not written          (twin: written)
      --only versions --notify --ci        notify: 0 calls           (twin: 1)
      --only versions --json --ci          stdout: not JSON          (twin: one doc)
      --only versions --diff-report --ci   report: not printed       (twin: printed)

    Consumers measured before changing the stdout shape: docs-ci.yaml's
    `drift-checks` (`--only ... --ci`) and the Makefile's `lint-docs`
    (`--only ... $(ARGS)`) both read the exit code only; a grep across
    .github/, Makefile, docs/ and scripts/ finds nothing parsing `--ci`
    stdout. The exit code is unchanged (1), now produced by the same
    `sys.exit(0 if failed == 0 else 1)` as every other mode.
    """

    def _failing(self, monkeypatch, status="fail"):
        ran = []

        def run_one(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, status, 0.2, "error detail", "failure output")
        monkeypatch.setattr(va, "_run_one", run_one)
        return ran

    @staticmethod
    def _three_names():
        """Three CONSECUTIVE names in TOOLS order, starting at the first
        fixable one. The run iterates TOOLS, not the `--only` string, so the
        "first" check has to be first in TOOLS order -- measured: with
        `versions,links,mermaid` the run stopped after `links`."""
        names = [n for n, _, _, _ in TOOLS]
        i = next(i for i, n in enumerate(names) if n in FIX_COMMANDS)
        picked = names[i:i + 3]
        assert len(picked) == 3, "the fix row below would be vacuous"
        assert "versions" in FIX_COMMANDS, "the single-check rows use it"
        return picked

    def _main(self, cli_argv, capsys, *argv):
        cli_argv('validate_all', *argv)
        with pytest.raises(SystemExit) as exc:
            va.main()
        captured = capsys.readouterr()
        return exc.value.code, captured.out, captured.err

    # ---- the issue's five rows, each with its non-`--ci` twin -----------

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_fix_runs_the_fix_subprocess_once(self, monkeypatch, capsys,
                                              cli_argv, ci):
        fix_calls = []

        def mock_sub(cmd, **kwargs):
            fix_calls.append(cmd)
            r = MagicMock()
            r.returncode = 0
            r.stdout = "Fixed something"
            return r
        self._failing(monkeypatch)
        monkeypatch.setattr(subprocess, "run", mock_sub)
        rc, out, _ = self._main(cli_argv, capsys, '--only', 'versions',
                                '--fix', *ci)
        assert rc == 1
        assert len(fix_calls) == 1, f"fix ran {len(fix_calls)} times"
        assert "Auto-fixing" in out

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_profile_writes_the_csv(self, monkeypatch, capsys, cli_argv,
                                    tmp_path, ci):
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        self._failing(monkeypatch)
        rc, _, _ = self._main(cli_argv, capsys, '--only', 'versions',
                              '--profile', *ci)
        assert rc == 1
        assert csv_file.exists(), "no CSV row was written"
        lines = csv_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2, "header + one row"
        assert "fail" in lines[1].split(",")

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_notify_notifies_once(self, monkeypatch, capsys, cli_argv, ci):
        calls = []
        monkeypatch.setattr(va, "_send_notification",
                            lambda t, m: calls.append((t, m)))
        self._failing(monkeypatch)
        rc, _, _ = self._main(cli_argv, capsys, '--only', 'versions',
                              '--notify', *ci)
        assert rc == 1
        assert len(calls) == 1, calls
        assert "Failed" in calls[0][0]

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_json_emits_one_document_on_failure(self, monkeypatch, capsys,
                                                cli_argv, ci):
        """The row that matters most: `--json --ci` produced NO JSON exactly
        when the run failed -- the one time a consumer would read it."""
        self._failing(monkeypatch)
        rc, out, _ = self._main(cli_argv, capsys, '--only', 'versions',
                                '--json', *ci)
        assert rc == 1
        data = json.loads(out)  # raises if stdout is not one document
        assert data["failed"] == 1
        assert data["results"]["versions"]["status"] == "fail"
        assert data["ci_stopped_after"] == ("versions" if ci else None)
        assert data["not_run"] == []
        assert "CI mode" not in out, "no banner text inside a JSON stdout"

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_diff_report_is_printed(self, monkeypatch, capsys, cli_argv, ci):
        self._failing(monkeypatch)
        monkeypatch.setattr(
            va, "_generate_diff_report",
            lambda failed, tools_dir, root: "=== DIFF REPORT ===\nversions")
        rc, out, _ = self._main(cli_argv, capsys, '--only', 'versions',
                                '--diff-report', *ci)
        assert rc == 1
        assert "DIFF REPORT" in out

    # ---- what a stop looks like when there WAS something left to run ----

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_a_stop_names_the_checks_not_run_in_the_text_summary(
            self, monkeypatch, capsys, cli_argv, ci):
        """With three failing checks, `--ci` runs the first only and the
        summary says so twice: the existing banner at the stop, and one
        line after `Result:` naming the two that were never reached. The
        twin runs all three and prints neither."""
        first, b, c = self._three_names()
        ran = self._failing(monkeypatch)
        rc, out, _ = self._main(cli_argv, capsys,
                                '--only', ",".join([first, b, c]), *ci)
        assert rc == 1
        if ci:
            assert ran == [first]
            assert f"CI mode: Stopping after failure ({first})" in out
            assert "Result: 0/3 passed, 1 failed" in out, out
            assert (f"CI mode: stopped after {first}; 2 check(s) not run: "
                    f"{b}, {c}") in out, out
        else:
            assert ran == [first, b, c]
            assert "CI mode" not in out
            assert "Result: 0/3 passed, 3 failed" in out, out

    @pytest.mark.parametrize("ci", [[], ["--ci"]], ids=["twin", "ci"])
    def test_json_shape_of_a_stop(self, monkeypatch, capsys, cli_argv, ci):
        """One pinned shape: `ci_stopped_after` and `not_run` are ALWAYS
        present (null / [] when nothing stopped). Not-run checks are NOT in
        `results`: a not-run check is not a result, and `--compare` reads a
        name absent from `results` as vanished (#1703) -- which is the
        right reading, so the two keys carry them instead. The identity
        `total == len(results) + len(not_run)` is what lets a consumer
        reconcile the counts."""
        first, b, c = self._three_names()
        self._failing(monkeypatch)
        rc, out, _ = self._main(cli_argv, capsys, '--json',
                                '--only', ",".join([first, b, c]), *ci)
        assert rc == 1
        data = json.loads(out)
        assert "ci_stopped_after" in data and "not_run" in data
        assert data["total"] == 3
        if ci:
            assert data["ci_stopped_after"] == first
            assert data["not_run"] == [b, c]
            assert list(data["results"]) == [first]
        else:
            assert data["ci_stopped_after"] is None
            assert data["not_run"] == []
            assert list(data["results"]) == [first, b, c]
        assert data["total"] == len(data["results"]) + len(data["not_run"])
        assert not set(data["not_run"]) & set(data["results"])

    def test_a_stop_on_an_error_status_is_still_exit_one(
            self, monkeypatch, capsys, cli_argv):
        """The stop fires on `error` as well as `fail` (timeouts, OSError),
        and `failed` counts both, so the tail's exit code is 1 -- a stopped
        run can never leave at exit 0."""
        first, b, c = self._three_names()
        ran = self._failing(monkeypatch, status="error")
        rc, out, _ = self._main(cli_argv, capsys, '--ci',
                                '--only', ",".join([first, b, c]))
        assert rc == 1
        assert ran == [first]
        assert f"stopped after {first}; 2 check(s) not run" in out

    def test_ci_with_every_check_passing_does_not_stop(
            self, monkeypatch, capsys, cli_argv):
        """Control: `--ci` on a green run is the plain sequential run --
        all three run, no banner, no `not run` line, exit 0, and the JSON
        keys are at their not-stopped values."""
        first, b, c = self._three_names()
        ran = []

        def ok(short_name, script_path, tool_args, project_root):
            ran.append(short_name)
            return (short_name, "pass", 0.1, "ok", "output")
        monkeypatch.setattr(va, "_run_one", ok)
        rc, out, _ = self._main(cli_argv, capsys, '--ci', '--json',
                                '--only', ",".join([first, b, c]))
        assert rc == 0
        assert ran == [first, b, c]
        data = json.loads(out)
        assert data["ci_stopped_after"] is None and data["not_run"] == []


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


def _args_attrs_read_by_run_watch():
    """Every ``args.<attr>`` read inside ``_run_watch``, from its AST.

    #1695 family 2: main() returns right after ``_run_watch``, so a flag this
    function does not read is a flag ``--watch`` silently drops. The set of
    flags it DOES read is derived here, from the function body, so that the
    conflict table in the tool (a pin) can be compared against it.
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(va._run_watch)))
    attrs = {node.attr for node in ast.walk(tree)
             if isinstance(node, ast.Attribute)
             and isinstance(node.value, ast.Name) and node.value.id == "args"}
    assert "skip" in attrs, (
        "the AST walk found no `args.skip` read in _run_watch, and that read "
        "is known to exist -- the derivation is broken, not the function")
    return attrs


def _flags_run_watch_never_reads():
    """Parser ``store_true`` flags (minus ``--list`` / ``--watch``) whose
    argparse dest ``_run_watch`` never reads. ``--only`` is a string flag and
    has its own, older pair."""
    read = _args_attrs_read_by_run_watch()
    return sorted(f for f in _BOOL_FLAGS
                  if f.lstrip("-").replace("-", "_") not in read)


_WATCH_BLIND = _flags_run_watch_never_reads()


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
                       only_set`). Guarded since #1695 family 1 — see
                       `TestConflictingFlags` below.
                     * EARLY RETURN: a branch exits before later flags are
                       read (the `--watch` dispatch; `--ci`'s exit inside the
                       run loop, which was ahead of the summary and of every
                       flag handled after it). Closed by #1695 family 2:
                       `--ci` now breaks out and falls through to the same
                       tail as every mode (`TestCiStopFallsThroughToTheTail`),
                       and `--watch` with a flag `_run_watch` never reads is
                       exit 2 (`TestConflictingFlags`).
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
# #1695 family 1: a flag pair where one flag silently loses
# ============================================================

class TestConflictingFlags:
    """A flag combination where one flag would be ignored is
    EXIT_CALLER_ERROR (#1695 family 1, and the `--watch` half of family 2;
    the `--ci` half is TestCiStopFallsThroughToTheTail).

    Measured on this branch's parent (mock `_run_one` recording the
    selection; `_run_watch` inspected statically), each pair alongside the
    control that had to behave identically and did:

      --smart --only versions        rc 0, ran ['versions']
      [control] --only versions      rc 0, ran ['versions']  — same bytes
      --parallel --ci                rc 0, sequential branch, header said
                                     PARALLEL
      --baseline --compare           rc 0, baseline written, no comparison
      --watch --only versions        `_run_watch` never reads only_set
      --watch + any of _WATCH_BLIND  `_run_watch` reads `args.skip` (and,
                                     since this change, `args.verbose`)
                                     and main() returns right after it, so
                                     --json / --fix / --profile / --notify /
                                     --diff-report / --baseline / --compare
                                     / --smart / --ci / --parallel were all
                                     parsed and never consulted (family 2)

    Callers were measured BEFORE rejecting: the two automatic invocations
    (Makefile `lint-docs`: `--only … $(ARGS)`; docs-ci.yaml `drift-checks`:
    `--only … --ci`) pass none of these pairs, and a repo-wide grep found no
    other non-test invocation passing one.
    """

    _CALLER_ERROR = 2  # independent literal, same reason as TestUnknownCheckNames

    _PAIRS = [
        (["--smart", "--only", "versions"], "--smart", "--only"),
        (["--parallel", "--ci"], "--parallel", "--ci"),
        (["--baseline", "--compare"], "--baseline", "--compare"),
        (["--watch", "--only", "versions"], "--watch", "--only"),
    ]

    def _rec_run_one(self, short_name, script_path, tool_args, project_root):
        # A bound method, not a closure: the `--parallel` control below
        # goes through ProcessPoolExecutor, which pickles `_run_one`, and a
        # local function cannot be pickled (measured: AttributeError from
        # the pool). Appends made in a child process do not come back, so
        # `calls` is only asserted on in-process (rejected / sequential)
        # runs.
        self.calls.append(short_name)
        return (short_name, "pass", 0.1, "ok", "output")

    def _quiet(self, monkeypatch, tmp_path):
        """Make every mode inert so a control run cannot touch the tree or
        hang: no subprocess, no watch loop, no real baseline / CSV path."""
        self.calls = []
        self.entered = []
        monkeypatch.setattr(va, "_run_one", self._rec_run_one)
        monkeypatch.setattr(va, "_run_watch",
                            lambda *a, **k: self.entered.append("watch"))
        monkeypatch.setattr(
            va, "_smart_detect",
            lambda root: va.SmartSelection(["versions"], [], ["mkdocs.yml"]))
        monkeypatch.setattr(va, "BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(va, "PROFILE_CSV", tmp_path / "profile.csv")
        return self.calls, self.entered

    @pytest.mark.parametrize("argv,a,b", _PAIRS,
                             ids=[f"{a}+{b}" for _, a, b in _PAIRS])
    def test_each_pair_is_rejected_naming_both_flags(
            self, monkeypatch, capsys, cli_argv, tmp_path, argv, a, b):
        """Every pair → exit 2, nothing runs, and stderr names BOTH flags:
        the operator believed the losing flag was in effect, so the message
        has to say which one it was."""
        calls, entered = self._quiet(monkeypatch, tmp_path)
        cli_argv('validate_all', *argv)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == self._CALLER_ERROR, (
            f"{argv} was accepted; before #1695 it ran at rc 0 with one "
            f"flag silently dropped")
        assert calls == [] and entered == [], (
            "a rejected invocation must not run anything")
        err = capsys.readouterr().err
        assert a in err and b in err, err
        assert "would be ignored" in err, err
        err.encode("ascii")  # stderr is not utf-8-patched (see #1620)

    @pytest.mark.parametrize("argv", [
        ["--smart"], ["--only", "versions"], ["--parallel"], ["--ci"],
        ["--baseline"], ["--compare"], ["--watch", "--skip", "versions"],
    ], ids=lambda a: "+".join(a))
    def test_each_flag_alone_does_not_hit_the_guard(
            self, monkeypatch, capsys, cli_argv, tmp_path, argv):
        """Paired control: an implementation that rejects any invocation
        carrying one of these flags satisfies the test above. Each flag on
        its own (with a valid name where one is needed) must get past the
        guard — `--watch` returns None from main(), the rest exit 0."""
        self._quiet(monkeypatch, tmp_path)
        cli_argv('validate_all', *argv)
        if "--watch" in argv:
            assert va.main() is None
        else:
            with pytest.raises(SystemExit) as exc:
                va.main()
            assert exc.value.code == 0, (
                f"{argv} alone must be accepted "
                f"(stderr: {capsys.readouterr().err.strip()[:200]!r})")
        assert "would be ignored" not in capsys.readouterr().err

    def test_an_empty_only_is_no_restriction_and_no_conflict(
            self, monkeypatch, capsys, cli_argv, tmp_path):
        """`--only ""` parses to zero names, which #1620 pins as "no
        restriction". The conflict predicate has to read the PARSED set,
        not the raw string: `--smart --only ""` selects by git diff, and is
        not a conflict."""
        calls, _ = self._quiet(monkeypatch, tmp_path)
        cli_argv('validate_all', '--smart', '--only', '')
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert calls == ["versions"], "the --smart selection was used"

    @staticmethod
    def _namespace(**on):
        """Every parser boolean off (derived from the parser, so a new flag
        is present here too), then the named ones on."""
        import argparse
        base = {f.lstrip("-").replace("-", "_"): False
                for f in _BOOL_FLAGS + ["--watch", "--list"]}
        return argparse.Namespace(**{**base, **on})

    def test_the_guard_is_derived_from_args_the_dispatch_reads(self):
        """The four sites named in #1695 family 1 are the four pairs, and the
        predicate for `--only` is `only_set`, not `args.only` — asserted
        on the function so the parametrize above cannot quietly cover
        three pairs while a fourth is dropped from the guard."""
        assert va._conflicting_flags(self._namespace(), set()) == []
        assert va._conflicting_flags(self._namespace(), {"versions"}) == []
        got = va._conflicting_flags(self._namespace(
            smart=True, parallel=True, ci=True, baseline=True, compare=True,
            watch=True), {"versions"})
        assert [(k, i) for k, i, _ in got][:4] == [
            ("--only", "--smart"), ("--ci", "--parallel"),
            ("--baseline", "--compare"), ("--watch", "--only")]
        # `--smart --only ""`: an empty set is no conflict
        assert va._conflicting_flags(self._namespace(smart=True), set()) == []

    # ---- #1695 family 2, the --watch half -------------------------------

    def test_the_watch_pin_equals_the_set_derived_from_run_watch(self):
        """⛔ Two independent sources. The tool carries `_WATCH_NEVER_READS`
        as a PIN (dest -> flag); this file derives the same set from the
        parser's store_true flags minus the `args.<attr>` reads in
        `_run_watch`'s own AST. They have to agree in both directions: a
        flag added to the parser that `_run_watch` does not read shows up
        derived-only (it would be dropped silently again -- the defect);
        a read added to `_run_watch` without removing the pin shows up
        pin-only (a legal flag is being rejected -- the false red that gets
        guards deleted)."""
        derived = set(_WATCH_BLIND)
        pinned = set(va._WATCH_NEVER_READS.values())
        assert derived, "derived nothing; the parametrize below is vacuous"
        assert pinned == derived, (
            f"pin-only={sorted(pinned - derived)} "
            f"derived-only={sorted(derived - pinned)}")
        for dest, flag in va._WATCH_NEVER_READS.items():
            assert dest == flag.lstrip("-").replace("-", "_"), (dest, flag)
        # And the reads the derivation subtracted are the ones the docstring
        # of _run_watch promises: skip and verbose, nothing else.
        assert _args_attrs_read_by_run_watch() == {"skip", "verbose"}

    @pytest.mark.parametrize("flag", _WATCH_BLIND, ids=lambda f: f.lstrip("-"))
    def test_watch_with_a_flag_it_never_reads_is_rejected(
            self, monkeypatch, capsys, cli_argv, tmp_path, flag):
        """Family 2: before this change every one of these ran at rc 0 and
        the other flag did nothing -- `--watch --json` printed the text
        banner, `--watch --notify` never notified, and so on. Exit 2, both
        flags named, watch never started."""
        calls, entered = self._quiet(monkeypatch, tmp_path)
        cli_argv('validate_all', '--watch', flag)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == self._CALLER_ERROR
        assert calls == [] and entered == []
        err = capsys.readouterr().err
        assert "--watch" in err and flag in err, err
        assert "never consulted" in err, err
        err.encode("ascii")

    @pytest.mark.parametrize("argv", [
        ["--watch", "--skip", "versions"], ["--watch", "--verbose"],
    ], ids=["skip", "verbose"])
    def test_watch_with_a_flag_it_reads_still_starts(
            self, monkeypatch, capsys, cli_argv, tmp_path, argv):
        """Paired control: the two flags `_run_watch` DOES read stay legal
        (an implementation rejecting `--watch` with anything at all passes
        the test above). `--verbose` is read since this change -- it used
        to be one more silently dropped flag, and printing full output is
        what a watch loop is for."""
        assert "verbose" in _args_attrs_read_by_run_watch()
        _, entered = self._quiet(monkeypatch, tmp_path)
        cli_argv('validate_all', *argv)
        assert va.main() is None
        assert entered == ["watch"]
        assert "would be ignored" not in capsys.readouterr().err


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
