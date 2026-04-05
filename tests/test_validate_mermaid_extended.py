"""Extended tests for validate_mermaid.py — coverage boost.

Targets: validate_file, render_with_mmdc, print_summary, main() CLI.
"""
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import validate_mermaid as vm


# ============================================================
# validate_file
# ============================================================
class TestValidateFile:
    """MermaidValidator.validate_file() tests."""

    def test_valid_diagram(self, tmp_path):
        md = tmp_path / "valid.md"
        md.write_text("# Title\n\n```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 1
        assert v.valid_diagrams == 1

    def test_file_read_error(self, tmp_path):
        """Non-readable file returns error."""
        md = tmp_path / "unreadable.md"
        md.write_text("content", encoding="utf-8")
        os.chmod(str(md), 0o000)
        v = vm.MermaidValidator()
        try:
            errors = v.validate_file(md)
            assert len(errors) == 1
            assert errors[0]["status"] == "error"
        finally:
            os.chmod(str(md), 0o644)

    def test_unclosed_mermaid_block(self, tmp_path):
        md = tmp_path / "unclosed.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n",
                      encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert len(errors) == 1
        assert "Unclosed" in errors[0]["message"]

    def test_empty_mermaid_block(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("```mermaid\n```\n", encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert v.total_diagrams == 0

    def test_multiple_diagrams(self, tmp_path):
        md = tmp_path / "multi.md"
        md.write_text(
            "```mermaid\ngraph TD\n    A-->B\n```\n\n"
            "```mermaid\nsequenceDiagram\n    A->>B: Hello\n```\n",
            encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 2
        assert v.valid_diagrams == 2

    def test_diagram_with_syntax_error(self, tmp_path):
        md = tmp_path / "bad.md"
        md.write_text(
            "```mermaid\ngraph TD\n    subgraph Group\n    A-->B\n```\n",
            encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert len(errors) == 1
        assert "subgraph" in errors[0]["message"]

    def test_no_mermaid_blocks(self, tmp_path):
        md = tmp_path / "plain.md"
        md.write_text("# No diagrams\n\nJust text.\n", encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 0

    def test_verbose_mode(self, tmp_path, capsys):
        md = tmp_path / "verbose.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        v = vm.MermaidValidator(verbose=True)
        v.validate_file(md)
        # verbose mode prints checkmark


# ============================================================
# render_with_mmdc
# ============================================================
class TestRenderWithMmdc:
    """MermaidValidator.render_with_mmdc() tests."""

    def test_mmdc_not_found(self, tmp_path, monkeypatch):
        """If mmdc is not found, returns True (skips)."""
        def mock_run(cmd, **kwargs):
            raise FileNotFoundError("mmdc not found")
        monkeypatch.setattr(subprocess, "run", mock_run)

        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        v = vm.MermaidValidator()
        result = v.render_with_mmdc(md)
        assert result is True

    def test_mmdc_timeout(self, tmp_path, monkeypatch):
        """If mmdc version check times out, returns True (skips)."""
        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5)
        monkeypatch.setattr(subprocess, "run", mock_run)

        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        v = vm.MermaidValidator()
        result = v.render_with_mmdc(md)
        assert result is True


# ============================================================
# print_summary
# ============================================================
class TestPrintSummary:
    """MermaidValidator.print_summary() tests."""

    def test_no_errors(self, capsys):
        v = vm.MermaidValidator()
        v.total_diagrams = 5
        v.valid_diagrams = 5
        v.print_summary()
        out = capsys.readouterr().out
        assert "Total diagrams found: 5" in out
        assert "Valid diagrams: 5" in out

    def test_with_errors(self, capsys):
        v = vm.MermaidValidator()
        v.total_diagrams = 3
        v.valid_diagrams = 1
        v.errors = [
            {"file": "test.md", "line": 5, "diagram_type": "graph",
             "status": "error", "message": "bad syntax"},
            {"file": "test.md", "line": 15, "diagram_type": "flowchart",
             "status": "error", "message": "unmatched"},
        ]
        v.print_summary()
        combined = capsys.readouterr()
        assert "Total diagrams found: 3" in combined.out
        assert "Errors found: 2" in combined.err


# ============================================================
# _check_brackets
# ============================================================
class TestCheckBrackets:
    def test_matched(self):
        v = vm.MermaidValidator()
        assert v._check_brackets("A[text] --> B(text)") == []

    def test_unmatched_opening(self):
        v = vm.MermaidValidator()
        errors = v._check_brackets("A[text")
        assert len(errors) == 1
        assert "Unmatched opening" in errors[0]

    def test_unmatched_closing(self):
        v = vm.MermaidValidator()
        errors = v._check_brackets("text]")
        assert len(errors) == 1
        assert "Unmatched closing" in errors[0]


# ============================================================
# _check_duplicate_ids
# ============================================================
class TestCheckDuplicateIds:
    def test_no_duplicates(self):
        v = vm.MermaidValidator()
        content = "A[Node A]\nB[Node B]"
        assert v._check_duplicate_ids(content) == []

    def test_with_duplicates(self):
        v = vm.MermaidValidator()
        content = "A[Node A]\nB[Node B]\nA[Duplicate A]"
        errors = v._check_duplicate_ids(content)
        assert len(errors) == 1
        assert "Duplicate" in errors[0]


# ============================================================
# _check_arrow_syntax
# ============================================================
class TestCheckArrowSyntax:
    def test_valid_arrows(self):
        v = vm.MermaidValidator()
        content = "A --> B\nC ==> D\nE -.-> F"
        assert v._check_arrow_syntax(content) == []

    def test_invalid_arrow(self):
        v = vm.MermaidValidator()
        content = "A - > B"
        errors = v._check_arrow_syntax(content)
        assert len(errors) == 1
        assert "Invalid arrow" in errors[0]

    def test_comment_lines_skipped(self):
        v = vm.MermaidValidator()
        content = "%% comment\n"
        assert v._check_arrow_syntax(content) == []


# ============================================================
# main() CLI
# ============================================================
class TestMainCLI:
    """validate_mermaid main() CLI tests."""

    def test_main_with_valid_file(self, tmp_path, monkeypatch, capsys):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", str(md)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found: 1" in combined.out

    def test_main_with_directory(self, tmp_path, monkeypatch, capsys):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", str(tmp_path)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found:" in combined.out

    def test_main_nonexistent_path(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", "/nonexistent/path"
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 1

    def test_main_no_md_files(self, tmp_path, monkeypatch, capsys):
        """Directory with no .md files."""
        (tmp_path / "readme.txt").write_text("not markdown", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", str(tmp_path)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0

    def test_main_ci_with_errors(self, tmp_path, monkeypatch, capsys):
        md = tmp_path / "bad.md"
        md.write_text(
            "```mermaid\ngraph TD\n    subgraph X\n    A-->B\n```\n",
            encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", "--ci", str(md)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 1

    def test_main_verbose(self, tmp_path, monkeypatch, capsys):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", "--verbose", str(md)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0

    def test_main_single_non_md_file(self, tmp_path, monkeypatch, capsys):
        """Single file that's not .md."""
        f = tmp_path / "test.txt"
        f.write_text("not markdown", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "validate_mermaid", str(f)
        ])
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
