"""check_jsx_linecount.py — the line-count guard (issue #152), pure Python since #3669.

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

import check_jsx_linecount as ljb  # noqa: E402


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
            ljb._compute_exit_code(ci=True, strict=False, hard=[], soft=issues)
            == 0
        )

    def test_2501_lines_fails_under_ci_strict(self):
        # Verbatim acceptance criterion from issue #152:
        # "a 2501-line .jsx fails under `--ci --strict`".
        # It also fails under `--ci` alone: a hard-cap file is always
        # fatal under --ci. Both modes are tested.
        src = self._make_source(2501)
        issues = ljb._run_line_count_check("foo.jsx", src)
        assert len(issues) == 1
        assert issues[0]["severity"] == "hard"
        for strict_flag in (False, True):
            assert (
                ljb._compute_exit_code(ci=True, strict=strict_flag, hard=issues, soft=[])
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
# Issue body shape — main() reads these keys
# ---------------------------------------------------------------------------
class TestIssueDictShape:
    def test_required_keys_present(self):
        src = "\n".join(f"x = {i}" for i in range(2000)) + "\n"
        issues = ljb._run_line_count_check("interactive/big.jsx", src)
        assert len(issues) == 1
        for key in ("path", "severity", "error"):
            assert key in issues[0]
        assert issues[0]["path"] == "interactive/big.jsx"


# ---------------------------------------------------------------------------
# _compute_exit_code — without --ci report only; --ci: hard fatal;
# --ci --strict: soft fatal too.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ci,strict,hard,soft,want", [
    (False, False, False, False, 0),
    (False, False, True, True, 0),
    (False, True, True, True, 0),     # --strict without --ci is report only
    (True, False, False, False, 0),
    (True, False, True, False, 1),
    (True, False, False, True, 0),    # soft cap warns without --strict
    (True, False, True, True, 1),
    (True, True, False, False, 0),
    (True, True, False, True, 1),
    (True, True, True, False, 1),
])
def test_compute_exit_code(ci, strict, hard, soft, want):
    item = [{"path": "x", "severity": "x", "error": "x"}]
    assert ljb._compute_exit_code(ci=ci, strict=strict, hard=item if hard else [],
                                  soft=item if soft else []) == want


# ---------------------------------------------------------------------------
# The CLI, end to end (no node: #3669 — the guard never needs a toolchain)
# ---------------------------------------------------------------------------
_SCRIPT = os.path.join(_TOOLS_DIR, "check_jsx_linecount.py")
_ENV = {k: v for k, v in os.environ.items() if k not in ("PYTHONUNBUFFERED",
                                                         "DA_TEST_CRASH_EXIT_CODE")}


def _cli(tmp_path, files, *flags, path_env=None):
    import subprocess
    d = tmp_path / "tree"
    d.mkdir(exist_ok=True)
    for name, n in files.items():
        f = d / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(f"// {i}\n" for i in range(n)), encoding="utf-8")
    env = dict(_ENV, PATH=path_env) if path_env is not None else _ENV
    return subprocess.run([sys.executable, _SCRIPT, *flags, str(d)], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, env=env)


@pytest.mark.parametrize("flags,want", [((), 0), (("--ci",), 1), (("--ci", "--strict"), 1)])
def test_a_hard_cap_file(tmp_path, flags, want):
    proc = _cli(tmp_path, {"big.jsx": 2501, "ok.jsx": 10}, *flags)
    assert proc.returncode == want, proc.stdout + proc.stderr
    assert "big.jsx: (line-count/hard) 2501 lines" in proc.stdout


@pytest.mark.parametrize("flags,want", [((), 0), (("--ci",), 0), (("--ci", "--strict"), 1)])
def test_a_soft_cap_file(tmp_path, flags, want):
    proc = _cli(tmp_path, {"sub/mid.jsx": 1501}, *flags)
    assert proc.returncode == want, proc.stdout + proc.stderr
    assert "mid.jsx: (line-count/soft) 1501 lines" in proc.stdout  # rglob: subdirs too


def test_every_finding_is_printed(tmp_path):
    proc = _cli(tmp_path, {"a.jsx": 2600, "b.jsx": 2700, "c.jsx": 1600, "d.jsx": 1700},
                "--ci", "--strict")
    assert proc.returncode == 1
    for name in ("a.jsx", "b.jsx", "c.jsx", "d.jsx"):
        assert f"{name}: (line-count/" in proc.stdout, proc.stdout


def test_no_node_is_needed(tmp_path):
    """An empty PATH: the verdict stands (it was rc 2 with the Babel pass)."""
    proc = _cli(tmp_path, {"big.jsx": 2501}, "--ci", path_env=str(tmp_path / "empty"))
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_a_tree_with_no_jsx_is_no_verdict(tmp_path):
    proc = _cli(tmp_path, {"x.js": 10}, "--ci")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "ERROR:" in proc.stderr


def test_a_missing_directory_is_named(tmp_path):
    import subprocess
    proc = subprocess.run([sys.executable, _SCRIPT, "--ci", str(tmp_path / "nope")],
                          capture_output=True, text=True, encoding="utf-8", timeout=60, env=_ENV)
    assert proc.returncode == 2
    assert "nope" in proc.stderr


def test_the_real_tree_is_within_the_hard_cap():
    import subprocess
    proc = subprocess.run([sys.executable, _SCRIPT, "--ci", "--strict"], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, env=_ENV)
    assert proc.returncode == 0, proc.stdout + proc.stderr


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
