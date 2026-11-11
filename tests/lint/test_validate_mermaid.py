"""Tests for validate_mermaid.py — Mermaid diagram validation.

Merged from previous _extended split (PR test-refactor sweep): syntax-detection
helpers (TestDetectDiagramType / TestCheckSyntax / TestValidateFile / TestConstants)
sit alongside renderer + summary + bracket / duplicate-id / arrow / main()
coverage classes appended below.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

import validate_mermaid as vm
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


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

    @pytest.mark.parametrize("content", [None, b"```mermaid\n\xff\xfe\n```\n"],
                             ids=["missing", "not-utf8"])
    def test_an_unreadable_file_is_not_checked_not_an_error(self, tmp_path,
                                                            content):
        """No verdict, so no diagram error: it is recorded as not checked (#3577)."""
        md = tmp_path / "f.md"
        if content is not None:
            md.write_bytes(content)
        v = vm.MermaidValidator()
        assert v.validate_file(md) == []
        assert v.errors == []
        assert len(v.not_checked) == 1

    def test_main_exits_2_when_something_was_not_checked(self, tmp_path,
                                                         cli_argv):
        """Even next to a real diagram error, and with or without --ci: the run
        did not get a verdict, which is the contract's 2 (#3577, #3611)."""
        (tmp_path / "bad.md").write_text("```mermaid\nnotadiagram\n```\n",
                                         encoding="utf-8")
        (tmp_path / "z.md").write_bytes(b"\xff\xfe")
        for argv in ([str(tmp_path)], ["--ci", str(tmp_path)]):
            cli_argv("validate_mermaid", *argv)
            with pytest.raises(SystemExit) as exc:
                vm.main()
            assert exc.value.code == EXIT_CALLER_ERROR, argv

    # ── unique edge cases (merged from _extended) ───────────────────────

    @pytest.mark.skipif(
        sys.platform == "win32"
        or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="os.chmod(0o000) is a no-op in two environments where the read "
               "permission bit is bypassed and the file stays readable: "
               "(1) Windows NTFS uses ACLs, not POSIX mode bits — the chmod "
               "call is silently dropped; "
               "(2) Linux root (the dev-container default user, plus most CI "
               "runners) — the kernel grants superuser access regardless of "
               "mode bits. The test's read-error path simply cannot be exercised "
               "in either case. Caught when PR #482's full pytest run tripped "
               "this assertion under docker-root.",
    )
    def test_file_read_error(self, tmp_path):
        """A non-readable file is not checked, not a diagram error (#3577)."""
        md = tmp_path / "unreadable.md"
        md.write_text("content", encoding="utf-8")
        os.chmod(str(md), 0o000)
        v = vm.MermaidValidator()
        try:
            assert v.validate_file(md) == []
            assert v.errors == []
            assert len(v.not_checked) == 1
            assert "Permission denied" in v.not_checked[0]
        finally:
            os.chmod(str(md), 0o644)

    def test_empty_mermaid_block(self, tmp_path):
        md = tmp_path / "empty.md"
        md.write_text("```mermaid\n```\n", encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert v.total_diagrams == 0

    def test_diagram_with_syntax_error(self, tmp_path):
        md = tmp_path / "bad.md"
        md.write_text(
            "```mermaid\ngraph TD\n    subgraph Group\n    A-->B\n```\n",
            encoding="utf-8")
        v = vm.MermaidValidator()
        errors = v.validate_file(md)
        assert len(errors) == 1
        assert "subgraph" in errors[0]["message"]

    def test_verbose_mode(self, tmp_path, capsys):
        md = tmp_path / "verbose.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        v = vm.MermaidValidator(verbose=True)
        v.validate_file(md)
        # verbose mode prints checkmark


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate MermaidValidator constants."""

    def test_diagram_types_coverage(self):
        expected = {"graph", "flowchart", "sequenceDiagram", "classDiagram",
                    "stateDiagram", "erDiagram", "gantt", "pie"}
        assert expected.issubset(vm.MermaidValidator.DIAGRAM_TYPES)


# ---------------------------------------------------------------------------
# Renderer + summary + structural-check coverage
# (was test_validate_mermaid_extended.py)
# ---------------------------------------------------------------------------


#: A stand-in mmdc keeping mermaid-cli 12.2.0's measured contract: a diagram
#: it renders → exit 0 and a non-empty image at -o; a diagram it cannot parse
#: → exit 1; a renderer that cannot start → exit 1 for EVERY diagram, the
#: known-good one included. "Renderer broken" is switched on by a file
#: (BROKEN_AFTER: the number of renders that still work), so a test can kill
#: the renderer part-way through a run.
_STAND_IN_MMDC = """#!/bin/sh
in=""; out=""
while [ $# -gt 0 ]; do
  case "$1" in -i) in="$2"; shift;; -o) out="$2"; shift;; esac; shift
done
state="$(dirname "$0")/state"
n=$(cat "$state/count" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$state/count"
if [ -f "$state/broken_after" ] && [ $n -gt $(cat "$state/broken_after") ]; then
  echo 'Error: Browser was not found at the configured executablePath' >&2; exit 1
fi
if grep -q '((((' "$in"; then echo 'Error: Parse error on line 3' >&2; exit 1; fi
printf 'PNG' > "$out"
"""


def _mmdc(tmp_path, monkeypatch, broken_after=None):
    bin_dir = tmp_path / "bin"
    (bin_dir / "state").mkdir(parents=True)
    exe = bin_dir / "mmdc"
    exe.write_text(_STAND_IN_MMDC, encoding="utf-8", newline="\n")
    exe.chmod(0o755)
    if broken_after is not None:
        (bin_dir / "state" / "broken_after").write_text(str(broken_after), encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    return bin_dir / "state"


_GOOD = "```mermaid\ngraph TD\n    A-->B\n```\n"
_BAD = "```mermaid\nflowchart LR\n  A-->\n  ((((\n```\n"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stand-in for mmdc")
class TestRenderWithMmdc:
    """MermaidValidator.render_with_mmdc(), through the real subprocess path."""

    def _md(self, tmp_path, body):
        md = tmp_path / "test.md"
        md.write_text(body, encoding="utf-8")
        return md

    def test_no_mmdc_is_not_checked_not_a_pass(self, tmp_path, monkeypatch):
        """#3608: --render asked for and no renderer — it used to return True
        (a silent skip, rc 0). It is no verdict (rc 2), reported once."""
        monkeypatch.setenv("PATH", str(tmp_path))
        v = vm.MermaidValidator()
        md = self._md(tmp_path, _GOOD)
        assert v.render_with_mmdc(md) is False
        assert v.render_with_mmdc(md) is False
        assert len(v.not_checked) == 1 and "mmdc is not available" in v.not_checked[0]
        assert v.errors == []

    def test_a_renderer_that_cannot_start_is_not_checked_not_a_diagram_error(
            self, tmp_path, monkeypatch):
        """#3608 comment: mmdc present, Chrome not — exit 1 on every diagram.
        The canary fails first, so nothing is judged (it used to be rc 1)."""
        _mmdc(tmp_path, monkeypatch, broken_after=0)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is False
        assert v.errors == []
        assert len(v.not_checked) == 1 and "known-good diagram" in v.not_checked[0]

    def test_a_good_diagram_renders(self, tmp_path, monkeypatch):
        state = _mmdc(tmp_path, monkeypatch)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is True
        assert v.errors == [] and v.not_checked == []
        assert (state / "count").read_text().strip() == "2"   # canary + diagram

    def test_a_bad_diagram_is_a_finding_while_the_canary_still_renders(
            self, tmp_path, monkeypatch):
        _mmdc(tmp_path, monkeypatch)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _BAD)) is False
        assert v.not_checked == []
        assert len(v.errors) == 1 and "Parse error" in v.errors[0]["message"]

    def test_a_renderer_that_dies_part_way_is_not_checked(self, tmp_path, monkeypatch):
        """The canary renders (call 1), then the renderer stops: the diagram's
        exit 1 is followed by a failing canary, so it is not judged."""
        _mmdc(tmp_path, monkeypatch, broken_after=1)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is False
        assert v.errors == []
        assert len(v.not_checked) == 1 and "known-good diagram" in v.not_checked[0]

    def test_a_dead_renderer_is_reported_once_and_rendering_stops(
            self, tmp_path, monkeypatch):
        """After the canary fails once, no further diagram is rendered: 4
        diagrams with the renderer dying after the first canary cost the
        canary, one diagram and one re-canary — not a render + canary each."""
        state = _mmdc(tmp_path, monkeypatch, broken_after=1)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD * 4)) is False
        assert (state / "count").read_text().strip() == "3"
        assert len(v.not_checked) == 1 and v.errors == []

    def test_a_hung_renderer_is_stopped_after_one_diagram(self, tmp_path, monkeypatch):
        """#3649 review: a diagram that times out is followed by a canary;
        when that fails too, nothing more is rendered — 4 diagrams cost one
        canary, one diagram and one re-canary, not a timeout each."""
        _mmdc(tmp_path, monkeypatch)
        real_run = vm._lib_engine.run
        calls = []

        def run(located, sub, args, **kw):
            calls.append(args)
            if len(calls) == 1:
                return real_run(located, sub, args, **kw)   # first canary renders
            return vm._lib_engine.NoVerdict("mmdc did not finish within 30s (rc 124)")
        monkeypatch.setattr(vm._lib_engine, "run", run)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD * 4)) is False
        assert len(calls) == 3, calls
        assert len(v.not_checked) == 1 and "; and " in v.not_checked[0], v.not_checked
        assert v.errors == []

    def test_a_one_off_launch_failure_is_not_a_finding(self, tmp_path, monkeypatch):
        """#3649 review: the diagram exits 1 once (a transient Chromium launch
        failure), the canary then renders, and the diagram renders on the
        second attempt — no error. Only a diagram that fails twice around a
        good canary is a finding."""
        _mmdc(tmp_path, monkeypatch)
        real_run = vm._lib_engine.run
        calls = []

        def run(located, sub, args, **kw):
            calls.append(args)
            if len(calls) == 2:   # canary ok, then this diagram's first render
                return vm._lib_engine.Ran(False, subprocess.CompletedProcess(args, 1, "", "launch"))
            return real_run(located, sub, args, **kw)
        monkeypatch.setattr(vm._lib_engine, "run", run)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is True
        assert v.errors == [] and v.not_checked == []
        assert len(calls) == 4   # canary, diagram, re-canary, diagram again

    def test_a_file_without_diagrams_needs_no_renderer(self, tmp_path, monkeypatch):
        """No diagram, nothing to render: not even mmdc's absence matters."""
        monkeypatch.setenv("PATH", str(tmp_path))
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, "# no diagrams here\n")) is True
        assert v.not_checked == []

    def test_exit_0_without_an_image_is_not_checked(self, tmp_path, monkeypatch):
        state = _mmdc(tmp_path, monkeypatch)
        exe = state.parent / "mmdc"
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is False
        assert v.errors == []
        assert "no image" in v.not_checked[0]

    def test_a_render_timeout_is_not_checked_not_a_diagram_error(
            self, tmp_path, monkeypatch):
        """mmdc running out of time says nothing about the diagram (#3577)."""
        _mmdc(tmp_path, monkeypatch)
        real_run = vm._lib_engine.run
        calls = []

        def run(located, sub, args, **kw):
            calls.append(args)
            if len(calls) == 1:
                return real_run(located, sub, args, **kw)   # the canary
            return vm._lib_engine.NoVerdict("mmdc did not finish within 30s (rc 124)")
        monkeypatch.setattr(vm._lib_engine, "run", run)
        v = vm.MermaidValidator()
        assert v.render_with_mmdc(self._md(tmp_path, _GOOD)) is False
        assert v.errors == []
        assert len(v.not_checked) == 1 and "did not finish" in v.not_checked[0]


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


class TestMainCLI:
    """validate_mermaid main() CLI tests."""

    def test_main_with_valid_file(self, tmp_path, monkeypatch, capsys, cli_argv):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        cli_argv("validate_mermaid", str(md))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found: 1" in combined.out

    def test_main_with_directory(self, tmp_path, monkeypatch, capsys, cli_argv):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        cli_argv("validate_mermaid", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found:" in combined.out

    def test_main_with_two_directories(self, tmp_path, monkeypatch, capsys,
                                       cli_argv):
        """#1702: the registry passes two paths; before nargs='*' the second
        was `unrecognized arguments` → exit 2 on every run. The single-path
        cases around this one are the must-stay-green side (docs-ci).
        """
        for name in ("first", "second"):
            tree = tmp_path / name
            tree.mkdir()
            (tree / "d.md").write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                                       encoding="utf-8")
        cli_argv("validate_mermaid", str(tmp_path / "first"),
                 str(tmp_path / "second"))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found: 2" in combined.out, (
            "both paths must be scanned, not just the first")

    def test_main_overlapping_paths_do_not_double_count(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """Overlapping arguments must not scan the same file twice.

        The key is the path as constructed, not `.resolve()` -- see the note
        at the de-duplication site for why.
        """
        nested = tmp_path / "nested"
        nested.mkdir()
        (nested / "d.md").write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                                     encoding="utf-8")
        cli_argv("validate_mermaid", str(tmp_path), str(nested))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
        combined = capsys.readouterr()
        assert "Total diagrams found: 1" in combined.out, (
            "the shared file must be scanned once, not once per argument")

    def test_main_second_path_missing_is_a_caller_error(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """A bad path must not be swallowed just because the first one was fine."""
        good = tmp_path / "good"
        good.mkdir()
        (good / "d.md").write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                                   encoding="utf-8")
        cli_argv("validate_mermaid", str(good), str(tmp_path / "nope"))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == EXIT_CALLER_ERROR

    def test_main_nonexistent_path(self, monkeypatch, capsys, cli_argv):
        cli_argv("validate_mermaid", "/nonexistent/path")
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == EXIT_CALLER_ERROR  # #452: path not found

    def test_main_no_md_files(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Directory with no .md files."""
        (tmp_path / "readme.txt").write_text("not markdown", encoding="utf-8")
        cli_argv("validate_mermaid", str(tmp_path))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0

    def test_main_ci_with_errors(self, tmp_path, monkeypatch, capsys, cli_argv):
        md = tmp_path / "bad.md"
        md.write_text(
            "```mermaid\ngraph TD\n    subgraph X\n    A-->B\n```\n",
            encoding="utf-8")
        cli_argv("validate_mermaid", "--ci", str(md))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 1

    def test_main_verbose(self, tmp_path, monkeypatch, capsys, cli_argv):
        md = tmp_path / "test.md"
        md.write_text("```mermaid\ngraph TD\n    A-->B\n```\n",
                      encoding="utf-8")
        cli_argv("validate_mermaid", "--verbose", str(md))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0

    def test_main_single_non_md_file(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Single file that's not .md."""
        f = tmp_path / "test.txt"
        f.write_text("not markdown", encoding="utf-8")
        cli_argv("validate_mermaid", str(f))
        with pytest.raises(SystemExit) as exc:
            vm.main()
        assert exc.value.code == 0
