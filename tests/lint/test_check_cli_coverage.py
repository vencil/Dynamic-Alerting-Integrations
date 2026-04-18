#!/usr/bin/env python3
"""Tests for check_cli_coverage.py — CLI command coverage lint."""

import json
import os
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "lint"))

import check_cli_coverage as cc  # noqa: E402
from _lint_helpers import parse_command_map_keys  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_entrypoint(tmp_path):
    """Create a minimal entrypoint.py with COMMAND_MAP."""
    content = textwrap.dedent("""\
        COMMAND_MAP = {
            "check-alert": "check_alert.py",
            "diagnose": "diagnose.py",
            "scaffold": "scaffold_tenant.py",
        }
    """)
    p = tmp_path / "entrypoint.py"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def tmp_entrypoint_with_help(tmp_path):
    """Create entrypoint.py with COMMAND_MAP and help text."""
    content = textwrap.dedent("""\
        def _build_help_text(lang):
            if lang == 'zh':
                return \"\"\"
        Commands:
            check-alert       查詢
            diagnose          健康檢查
            scaffold          產生配置
        \"\"\"
            else:
                return \"\"\"
        Commands:
            check-alert       Query alert
            diagnose          Health check
            scaffold          Generate config
        \"\"\"

        COMMAND_MAP = {
            "check-alert": "check_alert.py",
            "diagnose": "diagnose.py",
            "scaffold": "scaffold_tenant.py",
        }
    """)
    p = tmp_path / "entrypoint.py"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def tmp_cheat_sheet(tmp_path):
    """Create a minimal cheat-sheet.md."""
    content = textwrap.dedent("""\
        | Command | Description |
        |---------|-------------|
        | `check-alert` | Query alert status |
        | `diagnose` | Health check |
        | `scaffold` | Generate config |
    """)
    p = tmp_path / "cheat-sheet.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def tmp_cli_reference(tmp_path):
    """Create a minimal cli-reference.md."""
    content = textwrap.dedent("""\
        ## Commands

        #### check-alert

        Description here.

        #### diagnose

        Description here.

        #### scaffold

        Description here.
    """)
    p = tmp_path / "cli-reference.md"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestParseCommandMap
# ---------------------------------------------------------------------------

class TestParseCommandMap:
    def test_basic(self, tmp_entrypoint):
        result = parse_command_map_keys(tmp_entrypoint)
        assert result == {"check-alert", "diagnose", "scaffold"}

    def test_with_comments(self, tmp_path):
        content = textwrap.dedent("""\
            COMMAND_MAP = {
                # Group A
                "check-alert": "check_alert.py",
                # Group C
                "lint": "lint_custom_rules.py",
            }
        """)
        p = tmp_path / "ep.py"
        p.write_text(content, encoding="utf-8")
        result = parse_command_map_keys(p)
        assert result == {"check-alert", "lint"}

    def test_empty_map(self, tmp_path):
        content = "COMMAND_MAP = {\n}\n"
        p = tmp_path / "ep.py"
        p.write_text(content, encoding="utf-8")
        result = parse_command_map_keys(p)
        assert result == set()

    def test_no_command_map(self, tmp_path):
        p = tmp_path / "ep.py"
        p.write_text("print('hello')\n", encoding="utf-8")
        result = parse_command_map_keys(p)
        assert result == set()


# ---------------------------------------------------------------------------
# TestParseHelpText
# ---------------------------------------------------------------------------

class TestParseHelpText:
    def test_basic(self, tmp_entrypoint_with_help):
        result = cc.parse_help_text_commands(tmp_entrypoint_with_help)
        assert "check-alert" in result
        assert "diagnose" in result
        assert "scaffold" in result

    def test_filters_non_commands(self, tmp_path):
        content = textwrap.dedent("""\
            text = \"\"\"
            Usage:
                da-tools <command>
            Commands:
                check-alert    query
            Global environment variables:
                PROMETHEUS_URL  endpoint
                DA_LANG         language
            \"\"\"
        """)
        p = tmp_path / "ep.py"
        p.write_text(content, encoding="utf-8")
        result = cc.parse_help_text_commands(p)
        assert "check-alert" in result
        assert "PROMETHEUS_URL" not in result
        assert "DA_LANG" not in result
        assert "Usage" not in result


# ---------------------------------------------------------------------------
# TestParseCheatSheet
# ---------------------------------------------------------------------------

class TestParseCheatSheet:
    def test_basic(self, tmp_cheat_sheet):
        result = cc.parse_cheat_sheet_commands(tmp_cheat_sheet)
        assert result == {"check-alert", "diagnose", "scaffold"}

    def test_header_row_excluded(self, tmp_path):
        content = "| Command | Description |\n|---------|-------------|\n"
        p = tmp_path / "cs.md"
        p.write_text(content, encoding="utf-8")
        result = cc.parse_cheat_sheet_commands(p)
        assert result == set()

    def test_missing_file(self, tmp_path):
        result = cc.parse_cheat_sheet_commands(tmp_path / "nope.md")
        assert result == set()


# ---------------------------------------------------------------------------
# TestParseCliReference
# ---------------------------------------------------------------------------

class TestParseCliReference:
    def test_basic(self, tmp_cli_reference):
        result = cc.parse_cli_reference_commands(tmp_cli_reference)
        assert result == {"check-alert", "diagnose", "scaffold"}

    def test_ignores_h2_and_h3(self, tmp_path):
        content = textwrap.dedent("""\
            ## Commands
            ### Prometheus API
            #### check-alert
            desc
        """)
        p = tmp_path / "cr.md"
        p.write_text(content, encoding="utf-8")
        result = cc.parse_cli_reference_commands(p)
        assert result == {"check-alert"}

    def test_missing_file(self, tmp_path):
        result = cc.parse_cli_reference_commands(tmp_path / "nope.md")
        assert result == set()


# ---------------------------------------------------------------------------
# TestCheckCoverage
# ---------------------------------------------------------------------------

class TestCheckCoverage:
    def test_full_coverage(self):
        cm = {"check-alert", "diagnose"}
        doc = {"check-alert", "diagnose"}
        errors = cc.check_coverage(cm, doc, "test-doc")
        assert errors == []

    def test_missing_command(self):
        cm = {"check-alert", "diagnose", "scaffold"}
        doc = {"check-alert", "diagnose"}
        errors = cc.check_coverage(cm, doc, "test-doc")
        assert len(errors) == 1
        assert errors[0][0] == "error"
        assert "scaffold" in errors[0][1]

    def test_extra_command(self):
        cm = {"check-alert"}
        doc = {"check-alert", "old-cmd"}
        errors = cc.check_coverage(cm, doc, "test-doc")
        assert len(errors) == 1
        assert errors[0][0] == "warning"
        assert "old-cmd" in errors[0][1]

    def test_both_missing_and_extra(self):
        cm = {"check-alert", "diagnose"}
        doc = {"check-alert", "old-cmd"}
        errors = cc.check_coverage(cm, doc, "test-doc")
        assert len(errors) == 2


# ---------------------------------------------------------------------------
# TestBilingualConsistency
# ---------------------------------------------------------------------------

class TestBilingualConsistency:
    def test_consistent(self):
        zh = {"check-alert", "diagnose"}
        en = {"check-alert", "diagnose"}
        errors = cc.check_bilingual_consistency(zh, en, "test-pair")
        assert errors == []

    def test_zh_only(self):
        zh = {"check-alert", "diagnose"}
        en = {"check-alert"}
        errors = cc.check_bilingual_consistency(zh, en, "test-pair")
        assert len(errors) == 1
        assert "diagnose" in errors[0][1]
        assert "in zh but missing in en" in errors[0][1]

    def test_en_only(self):
        zh = {"check-alert"}
        en = {"check-alert", "scaffold"}
        errors = cc.check_bilingual_consistency(zh, en, "test-pair")
        assert len(errors) == 1
        assert "scaffold" in errors[0][1]
        assert "in en but missing in zh" in errors[0][1]


# ---------------------------------------------------------------------------
# TestOutputFormatting
# ---------------------------------------------------------------------------

class TestOutputFormatting:
    def test_text_all_pass(self):
        report = cc.format_text_report([], {"a", "b"})
        assert "All commands covered" in report
        assert "2" in report

    def test_text_with_errors(self):
        errors = [("error", "missing cmd"), ("warning", "extra cmd")]
        report = cc.format_text_report(errors, {"a", "b"})
        assert "✗" in report
        assert "⊘" in report
        assert "1 error(s)" in report
        assert "1 warning(s)" in report

    def test_json_pass(self):
        report = cc.format_json_report([], {"a", "b"})
        data = json.loads(report)
        assert data["status"] == "pass"
        assert data["command_count"] == 2
        assert data["error_count"] == 0

    def test_json_fail(self):
        errors = [("error", "missing cmd")]
        report = cc.format_json_report(errors, {"a"})
        data = json.loads(report)
        assert data["status"] == "fail"
        assert data["error_count"] == 1


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_main_no_ci_always_zero(self, monkeypatch, capsys):
        """Without --ci, main() exits 0 even with errors (display-only mode)."""
        monkeypatch.setattr(cc, "ENTRYPOINT_PATH",
                            cc.Path("/nonexistent"))
        monkeypatch.setattr(sys, "argv", ["check_cli_coverage.py"])
        with pytest.raises(SystemExit) as exc_info:
            cc.main()
        assert exc_info.value.code == 0

    def test_main_ci_with_errors(self, monkeypatch, capsys):
        """With --ci and errors, main() exits 1."""
        monkeypatch.setattr(cc, "ENTRYPOINT_PATH",
                            cc.Path("/nonexistent"))
        monkeypatch.setattr(sys, "argv", ["check_cli_coverage.py", "--ci"])
        with pytest.raises(SystemExit) as exc_info:
            cc.main()
        assert exc_info.value.code == 1

    def test_main_json_flag(self, monkeypatch, capsys):
        """Test main() with --json flag."""
        monkeypatch.setattr(cc, "ENTRYPOINT_PATH",
                            cc.Path("/nonexistent"))
        monkeypatch.setattr(sys, "argv",
                            ["check_cli_coverage.py", "--json"])
        with pytest.raises(SystemExit):
            cc.main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["check"] == "cli_coverage"


# ---------------------------------------------------------------------------
# TestIntegration — uses real project files
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration tests using actual project files (skip if not in repo)."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_repo(self):
        if not cc.ENTRYPOINT_PATH.exists():
            pytest.skip("Not running inside project repo")

    def test_parse_real_command_map(self):
        cmds = parse_command_map_keys(cc.ENTRYPOINT_PATH)
        assert len(cmds) >= 20, f"Expected >=20 commands, got {len(cmds)}"
        assert "check-alert" in cmds
        assert "scaffold" in cmds
        assert "threshold-recommend" in cmds
        assert "test-notification" in cmds

    def test_real_cheat_sheets_consistent(self):
        zh = cc.parse_cheat_sheet_commands(cc.CHEAT_SHEET_ZH)
        en = cc.parse_cheat_sheet_commands(cc.CHEAT_SHEET_EN)
        # Should have the same commands
        assert zh == en, f"zh-only: {zh - en}, en-only: {en - zh}"

    def test_real_cli_references_consistent(self):
        zh = cc.parse_cli_reference_commands(cc.CLI_REF_ZH)
        en = cc.parse_cli_reference_commands(cc.CLI_REF_EN)
        assert zh == en, f"zh-only: {zh - en}, en-only: {en - zh}"

    def test_full_coverage_check(self):
        errors = cc.run_all_checks()
        error_msgs = [m for s, m in errors if s == "error"]
        assert error_msgs == [], (
            f"CLI coverage errors found:\n"
            + "\n".join(f"  - {m}" for m in error_msgs)
        )
