"""Unit tests for lint_jsx_babel.py line-count guard (issue #152).

Covers `_run_line_count_check` directly — pure function, no Node.js dep:
  - Below soft cap (1500): no issue
  - Exactly soft cap (1500): no issue (boundary is `>`, not `>=`)
  - Just over soft cap (1501): one soft-severity issue
  - At hard cap (2500): soft-severity (boundary is `>`, not `>=`)
  - Just over hard cap (2501): one hard-severity issue
  - Empty file: no issue
  - File without trailing newline: counted correctly (`wc -l`-style + 1)

Constants pinned so threshold drift triggers an explicit test failure
rather than a silent semantic change.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import lint_jsx_babel as ljb  # noqa: E402


# ---------------------------------------------------------------------------
# Constants are part of the contract — tests must explicitly notice if
# someone tweaks them. Issue #152's whole point is to codify these numbers.
# ---------------------------------------------------------------------------
class TestThresholdConstants:
    def test_soft_cap_is_1500(self):
        assert ljb.LINE_COUNT_WARN == 1500

    def test_hard_cap_is_2500(self):
        assert ljb.LINE_COUNT_FAIL == 2500

    def test_hard_above_soft(self):
        # Sanity — hard cap must be strictly greater than soft cap.
        assert ljb.LINE_COUNT_FAIL > ljb.LINE_COUNT_WARN


# ---------------------------------------------------------------------------
# _run_line_count_check — boundary behavior
# ---------------------------------------------------------------------------
class TestLineCountCheck:
    def _make_source(self, n_lines: int) -> str:
        """Build a source string with exactly n_lines lines (trailing newline)."""
        if n_lines == 0:
            return ""
        return "\n".join(f"// line {i + 1}" for i in range(n_lines)) + "\n"

    def test_empty_file_no_issue(self):
        issues = ljb._run_line_count_check("foo.jsx", "")
        assert issues == []

    def test_single_line_no_issue(self):
        issues = ljb._run_line_count_check("foo.jsx", "const x = 1;\n")
        assert issues == []

    def test_below_soft_cap_no_issue(self):
        # 100 lines — well under 1500
        src = self._make_source(100)
        assert ljb._run_line_count_check("foo.jsx", src) == []

    def test_at_soft_cap_no_issue(self):
        # Exactly 1500 lines — boundary is `>`, not `>=`
        src = self._make_source(ljb.LINE_COUNT_WARN)
        assert ljb._run_line_count_check("foo.jsx", src) == []

    def test_just_over_soft_cap_emits_soft_warning(self):
        # 1501 lines — first line over the soft cap
        src = self._make_source(ljb.LINE_COUNT_WARN + 1)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"
        assert "soft cap" in issues[0]["error"]
        assert str(ljb.LINE_COUNT_WARN) in issues[0]["error"]
        assert str(ljb.LINE_COUNT_WARN + 1) in issues[0]["error"]

    def test_well_above_soft_below_hard_emits_soft(self):
        # 2000 lines — middle of soft band
        src = self._make_source(2000)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"

    def test_at_hard_cap_emits_soft(self):
        # Exactly 2500 — still soft, boundary is `>`, not `>=`
        src = self._make_source(ljb.LINE_COUNT_FAIL)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"

    def test_just_over_hard_cap_emits_hard(self):
        # 2501 lines — first line over the hard cap
        src = self._make_source(ljb.LINE_COUNT_FAIL + 1)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "hard"
        assert "hard cap" in issues[0]["error"]
        assert "split" in issues[0]["error"].lower()
        # Reference to the decomposition pattern must be present so the
        # error message is actionable, not just diagnostic.
        assert "#153" in issues[0]["error"] or "PR-2d" in issues[0]["error"]

    def test_severely_over_hard_cap_emits_hard(self):
        # 5000 lines — well past the hard cap
        src = self._make_source(5000)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "hard"
        assert "5000" in issues[0]["error"]


# ---------------------------------------------------------------------------
# Trailing-newline counting — `wc -l` semantics
# ---------------------------------------------------------------------------
class TestTrailingNewline:
    def test_no_trailing_newline_counted(self):
        # 1501 actual lines, but no trailing newline (1500 newline chars).
        # We count the last line even without trailing newline so the user
        # sees the same number `wc -l` would (issue body cited 1671 / 1691).
        src = "\n".join(f"// line {i + 1}" for i in range(ljb.LINE_COUNT_WARN + 1))
        # No trailing "\n" appended.
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"

    def test_with_trailing_newline_counted_same(self):
        src = "\n".join(f"// line {i + 1}" for i in range(ljb.LINE_COUNT_WARN + 1)) + "\n"
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"


# ---------------------------------------------------------------------------
# Issue body shape — downstream main() switches on these keys
# ---------------------------------------------------------------------------
class TestIssueDictShape:
    def test_required_keys_present(self):
        src = "\n".join(f"x = {i}" for i in range(2000)) + "\n"
        issues = ljb._run_line_count_check("interactive/big.jsx", src)
        assert len(issues) == 1
        # main() iterates these keys to build the report — pin them.
        for key in ("path", "line", "severity", "error", "snippet"):
            assert key in issues[0]
        assert issues[0]["path"] == "interactive/big.jsx"
        assert issues[0]["line"] == 1  # line-count issues anchor at top of file


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
