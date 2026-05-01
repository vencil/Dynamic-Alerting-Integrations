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

    def test_2499_lines_warns_but_passes_in_default_ci(self):
        # Verbatim acceptance criterion from issue #152:
        # "a 2499-line file warns but passes" (under default --ci, without
        # --strict). 2499 is in the soft band (1500 < N ≤ 2500), so it
        # emits a soft warning. The severity matrix tests below verify
        # the exit-code follow-through.
        src = self._make_source(2499)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "soft"
        # And under --ci alone, soft is non-fatal (exit 0):
        assert (
            ljb._compute_exit_code(
                ci=True,
                strict=False,
                babel_failures=[],
                static_failures=[],
                linecount_hard_failures=[],
                linecount_soft_failures=issues,
            )
            == 0
        )

    def test_2501_lines_fails_under_ci_strict(self):
        # Verbatim acceptance criterion from issue #152:
        # "a 2501-line .jsx fails under `--ci --strict`".
        # (My impl deviates: it ALSO fails under `--ci` alone because
        # hard cap follows the Babel-parse-fatal-under-ci semantic
        # pattern. Both modes are tested.)
        src = self._make_source(2501)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "hard"
        for strict_flag in (False, True):
            assert (
                ljb._compute_exit_code(
                    ci=True,
                    strict=strict_flag,
                    babel_failures=[],
                    static_failures=[],
                    linecount_hard_failures=issues,
                    linecount_soft_failures=[],
                )
                == 1
            ), f"Hard cap should fail under --ci (strict={strict_flag})"

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


# ---------------------------------------------------------------------------
# _compute_exit_code — severity / exit-code matrix
#
# This is the contract the PR is paying for. Hard-cap fails under --ci
# (mirrors Babel parse), soft-cap only fails under --strict (mirrors static
# pattern warnings). Without --ci, the script is report-only.
# ---------------------------------------------------------------------------
class TestComputeExitCode:
    def _call(
        self,
        *,
        ci=False,
        strict=False,
        strict_static=False,
        strict_linecount=False,
        babel=False,
        static=False,
        lc_hard=False,
        lc_soft=False,
    ):
        """Convenience: pass booleans, get exit code.

        Each boolean controls whether that failure category has any items.
        The actual issue dicts don't matter — `_compute_exit_code` only
        looks at truthiness of the lists.
        """
        nonempty = [{"path": "x", "line": 1, "error": "x", "snippet": ""}]
        return ljb._compute_exit_code(
            ci=ci,
            strict=strict,
            strict_static=strict_static,
            strict_linecount=strict_linecount,
            babel_failures=nonempty if babel else [],
            static_failures=nonempty if static else [],
            linecount_hard_failures=nonempty if lc_hard else [],
            linecount_soft_failures=nonempty if lc_soft else [],
        )

    # --- Without --ci: report-only, NEVER exit 1 ---------------------------
    def test_no_ci_clean(self):
        assert self._call() == 0

    def test_no_ci_with_babel_failures_still_zero(self):
        # Even Babel parse errors don't fail without --ci — that's the
        # report-mode contract for local invocation.
        assert self._call(babel=True) == 0

    def test_no_ci_with_hard_cap_still_zero(self):
        assert self._call(lc_hard=True) == 0

    def test_no_ci_strict_with_failures_still_zero(self):
        # --strict alone (no --ci) is a no-op — design choice that matches
        # `--ci --strict` being the documented combination.
        assert self._call(
            strict=True, babel=True, static=True, lc_hard=True, lc_soft=True
        ) == 0

    # --- With --ci alone: babel + hard-cap fatal; static + soft-cap warn --
    def test_ci_clean_passes(self):
        assert self._call(ci=True) == 0

    def test_ci_babel_failure_fatal(self):
        assert self._call(ci=True, babel=True) == 1

    def test_ci_linecount_hard_fatal(self):
        # The whole point of issue #152 — hard cap blocks merges.
        assert self._call(ci=True, lc_hard=True) == 1

    def test_ci_static_warning_non_fatal(self):
        # style={{ }} pattern is a warning, not a blocker, in default mode.
        assert self._call(ci=True, static=True) == 0

    def test_ci_linecount_soft_non_fatal(self):
        # Soft cap warns; doesn't block — matches the v2.8.0 transition story.
        assert self._call(ci=True, lc_soft=True) == 0

    def test_ci_mixed_soft_only_non_fatal(self):
        # Static + soft together still non-fatal without --strict.
        assert self._call(ci=True, static=True, lc_soft=True) == 0

    def test_ci_hard_dominates_soft(self):
        # If hard fires, exit is 1 regardless of soft warnings.
        assert self._call(ci=True, lc_hard=True, lc_soft=True, static=True) == 1

    # --- With --ci --strict: ALL four categories fatal --------------------
    def test_ci_strict_clean_passes(self):
        assert self._call(ci=True, strict=True) == 0

    def test_ci_strict_static_fatal(self):
        # --strict elevates static pattern warnings to fatal.
        assert self._call(ci=True, strict=True, static=True) == 1

    def test_ci_strict_linecount_soft_fatal(self):
        # --strict elevates soft-cap line-count to fatal — same pattern.
        assert self._call(ci=True, strict=True, lc_soft=True) == 1

    def test_ci_strict_babel_fatal(self):
        # Babel parse errors stay fatal under --strict (contract preserved).
        assert self._call(ci=True, strict=True, babel=True) == 1

    def test_ci_strict_linecount_hard_fatal(self):
        # Hard cap stays fatal under --strict (contract preserved).
        assert self._call(ci=True, strict=True, lc_hard=True) == 1

    def test_ci_strict_all_failures_fatal(self):
        assert self._call(
            ci=True, strict=True, babel=True, static=True, lc_hard=True, lc_soft=True
        ) == 1


# ---------------------------------------------------------------------------
# Granular --strict-static / --strict-linecount flags (PR-A activation track)
#
# Activates partial strict mode without forcing all 330 pre-existing
# style={{}} violations to be fixed. Codebase has 0 line-count soft
# warnings after PR-2d Phase 3 (S#72), so --strict-linecount alone can
# be wired into CI's manual-stage hook RIGHT NOW as a regression gate
# without blocking on the static-pattern cleanup track.
# ---------------------------------------------------------------------------
class TestComputeExitCodeGranularStrict(TestComputeExitCode):
    """Re-uses parent's `_call` helper that supports the new flags."""

    # --- --strict-static alone: only static fatal, line-count still warn --
    def test_strict_static_alone_static_fatal(self):
        assert self._call(ci=True, strict_static=True, static=True) == 1

    def test_strict_static_alone_linecount_soft_non_fatal(self):
        # Static only — line-count soft stays as warn (the partial gate).
        assert self._call(ci=True, strict_static=True, lc_soft=True) == 0

    def test_strict_static_alone_clean_passes(self):
        assert self._call(ci=True, strict_static=True) == 0

    # --- --strict-linecount alone: line-count fatal, static still warn ---
    def test_strict_linecount_alone_lc_soft_fatal(self):
        # The PR-A activation case — linecount soft becomes fatal even
        # though style={{}} stays as warn. Codebase has 0 lc_soft today
        # so this is a regression gate, not a blocker.
        assert self._call(ci=True, strict_linecount=True, lc_soft=True) == 1

    def test_strict_linecount_alone_static_non_fatal(self):
        # PR-A's whole point — partial activation. 330 style={{}} stays
        # warn-only so this PR can ship without forcing static cleanup.
        assert self._call(ci=True, strict_linecount=True, static=True) == 0

    def test_strict_linecount_alone_clean_passes(self):
        assert self._call(ci=True, strict_linecount=True) == 0

    # --- Combined granular flags = same as legacy --strict ---
    def test_combined_granular_flags_same_as_legacy_strict(self):
        # Mathematical equivalence: --strict-static --strict-linecount
        # should produce identical exit codes to legacy --strict for
        # all failure-list combinations.
        from itertools import product
        for babel, static, lc_hard, lc_soft in product([False, True], repeat=4):
            legacy = self._call(
                ci=True, strict=True,
                babel=babel, static=static, lc_hard=lc_hard, lc_soft=lc_soft,
            )
            granular = self._call(
                ci=True, strict_static=True, strict_linecount=True,
                babel=babel, static=static, lc_hard=lc_hard, lc_soft=lc_soft,
            )
            assert legacy == granular, (
                f"Divergence at babel={babel} static={static} "
                f"lc_hard={lc_hard} lc_soft={lc_soft}: "
                f"legacy={legacy} granular={granular}"
            )

    # --- Hard cap + babel parse stay fatal regardless of flags ---
    def test_strict_linecount_alone_babel_still_fatal(self):
        # Babel parse always fatal under --ci.
        assert self._call(ci=True, strict_linecount=True, babel=True) == 1

    def test_strict_linecount_alone_hard_still_fatal(self):
        assert self._call(ci=True, strict_linecount=True, lc_hard=True) == 1

    def test_strict_static_alone_babel_still_fatal(self):
        assert self._call(ci=True, strict_static=True, babel=True) == 1

    def test_strict_static_alone_hard_still_fatal(self):
        assert self._call(ci=True, strict_static=True, lc_hard=True) == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
