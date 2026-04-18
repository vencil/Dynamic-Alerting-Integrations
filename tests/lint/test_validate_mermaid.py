"""Tests for validate_mermaid.py — Mermaid diagram validation."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import validate_mermaid as vm  # noqa: E402


# ---------------------------------------------------------------------------
# MermaidValidator — diagram type detection
# ---------------------------------------------------------------------------
class TestDetectDiagramType:
    """Tests for _detect_diagram_type()."""

    def setup_method(self):
        self.v = vm.MermaidValidator()

    def test_graph(self):
        assert self.v._detect_diagram_type("graph TD") == "graph"

    def test_flowchart(self):
        assert self.v._detect_diagram_type("flowchart LR") == "flowchart"

    def test_sequence_diagram(self):
        assert self.v._detect_diagram_type("sequenceDiagram") == "sequenceDiagram"

    def test_class_diagram(self):
        assert self.v._detect_diagram_type("classDiagram") == "classDiagram"

    def test_state_diagram(self):
        assert self.v._detect_diagram_type("stateDiagram-v2") == "stateDiagram"

    def test_er_diagram(self):
        assert self.v._detect_diagram_type("erDiagram") == "erDiagram"

    def test_pie_chart(self):
        assert self.v._detect_diagram_type('pie title "Distribution"') == "pie"

    def test_gantt(self):
        assert self.v._detect_diagram_type("gantt") == "gantt"

    def test_unknown_type(self):
        assert self.v._detect_diagram_type("not a diagram") == "unknown"

    def test_empty_string(self):
        assert self.v._detect_diagram_type("") == "unknown"


# ---------------------------------------------------------------------------
# MermaidValidator — syntax checking
# ---------------------------------------------------------------------------
class TestCheckSyntax:
    """Tests for _check_syntax()."""

    def setup_method(self):
        self.v = vm.MermaidValidator()

    def test_valid_graph(self):
        content = "graph TD\n    A-->B\n    B-->C"
        errors = self.v._check_syntax(content, "graph")
        assert errors == []

    def test_unmatched_subgraph(self):
        content = "graph TD\n    subgraph Group\n    A-->B"
        errors = self.v._check_syntax(content, "graph")
        assert any("subgraph/end" in e for e in errors)

    def test_matched_subgraph(self):
        content = "graph TD\n    subgraph Group\n    A-->B\n    end"
        errors = self.v._check_syntax(content, "graph")
        # Should not report subgraph/end mismatch
        assert not any("subgraph/end" in e for e in errors)

    def test_unmatched_single_quotes(self):
        content = "graph TD\n    A['broken"
        errors = self.v._check_syntax(content, "graph")
        assert any("single quote" in e.lower() for e in errors)

    def test_unmatched_double_quotes(self):
        content = 'graph TD\n    A["broken'
        errors = self.v._check_syntax(content, "graph")
        assert any("double quote" in e.lower() for e in errors)

    def test_valid_sequence(self):
        content = "sequenceDiagram\n    Alice->>Bob: Hello\n    Bob-->>Alice: Hi"
        errors = self.v._check_syntax(content, "sequenceDiagram")
        assert errors == []


# ---------------------------------------------------------------------------
# MermaidValidator — validate_file
# ---------------------------------------------------------------------------
class TestValidateFile:
    """Tests for validate_file()."""

    def test_valid_file(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(
            "# Title\n\n```mermaid\ngraph TD\n    A-->B\n```\n",
            encoding="utf-8",
        )
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 1
        assert v.valid_diagrams == 1

    def test_unclosed_block(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(
            "# Title\n\n```mermaid\ngraph TD\n    A-->B\n",
            encoding="utf-8",
        )
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert len(errors) == 1
        assert "Unclosed" in errors[0]["message"]

    def test_no_mermaid_blocks(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("# Title\n\nJust text.\n", encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 0

    def test_multiple_diagrams(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text(
            "```mermaid\ngraph TD\n    A-->B\n```\n\n"
            "```mermaid\nsequenceDiagram\n    Alice->>Bob: Hi\n```\n",
            encoding="utf-8",
        )
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert errors == []
        assert v.total_diagrams == 2
        assert v.valid_diagrams == 2

    def test_nonexistent_file(self, tmp_path):
        v = vm.MermaidValidator()
        errors = v.validate_file(tmp_path / "missing.md")
        assert len(errors) == 1
        assert errors[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate MermaidValidator constants."""

    def test_diagram_types_coverage(self):
        expected = {"graph", "flowchart", "sequenceDiagram", "classDiagram",
                    "stateDiagram", "erDiagram", "gantt", "pie"}
        assert expected.issubset(vm.MermaidValidator.DIAGRAM_TYPES)
