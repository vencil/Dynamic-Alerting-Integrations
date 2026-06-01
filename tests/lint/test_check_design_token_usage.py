#!/usr/bin/env python3
"""Tests for check_design_token_usage.py — Design token usage lint."""

import os
import subprocess
import sys
import textwrap

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "lint"))

import check_design_token_usage as dtu  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def jsx_file_clean(tmp_path):
    """JSX file using only design tokens (no violations)."""
    content = textwrap.dedent("""\
        export function Button({ label }) {
          return (
            <button
              style={{
                color: 'var(--da-color-text-primary)',
                backgroundColor: 'var(--da-color-bg-secondary)',
                padding: 'var(--da-space-2)',
                fontSize: 'var(--da-font-size-body)',
              }}
            >
              {label}
            </button>
          );
        }
    """)
    p = tmp_path / "clean.jsx"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def jsx_file_hex_violation(tmp_path):
    """JSX file with hardcoded hex color."""
    content = textwrap.dedent("""\
        export function Card() {
          return (
            <div style={{ backgroundColor: '#64748b' }}>
              Card content
            </div>
          );
        }
    """)
    p = tmp_path / "hex_violation.jsx"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def jsx_file_hex_exempt(tmp_path):
    """JSX file with hardcoded hex but marked with token-exempt."""
    content = textwrap.dedent("""\
        export function Gradient() {
          return (
            <div style={{ color: '#a0aec0' }}> /* token-exempt */
              Special gradient
            </div>
          );
        }
    """)
    p = tmp_path / "hex_exempt.jsx"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def jsx_file_hex_in_comment(tmp_path):
    """JSX file with hex color in comment (should be ignored)."""
    content = textwrap.dedent("""\
        export function Demo() {
          // Use #64748b for neutral shades
          return (
            <div>
              Demo
            </div>
          );
        }
    """)
    p = tmp_path / "hex_in_comment.jsx"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def jsx_file_px_violation(tmp_path):
    """JSX file with hardcoded px value in style."""
    content = textwrap.dedent("""\
        export function Text() {
          return (
            <span style={{ fontSize: '14px', lineHeight: '20px' }}>
              Body text
            </span>
          );
        }
    """)
    p = tmp_path / "px_violation.jsx"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def jsx_file_px_small(tmp_path):
    """JSX file with small px values (borders/hairlines, should be exempt)."""
    content = textwrap.dedent("""\
        export function Border() {
          return (
            <div style={{ borderWidth: '1px', borderRadius: '2px' }}>
              Hairline border
            </div>
          );
        }
    """)
    p = tmp_path / "px_small.jsx"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Tests for check_hardcoded_hex_colors
# ---------------------------------------------------------------------------

class TestHexColors:
    def test_clean_file(self, jsx_file_clean):
        content = jsx_file_clean.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, jsx_file_clean.name)
        assert issues == []

    def test_hex_color_violation(self, jsx_file_hex_violation):
        content = jsx_file_hex_violation.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, jsx_file_hex_violation.name)
        assert len(issues) == 1
        assert issues[0]["hex"] == "#64748b"
        assert issues[0]["line"] == 3

    def test_hex_exempt_comment(self, jsx_file_hex_exempt):
        content = jsx_file_hex_exempt.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, jsx_file_hex_exempt.name)
        assert issues == []

    def test_hex_in_comment(self, jsx_file_hex_in_comment):
        content = jsx_file_hex_in_comment.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, jsx_file_hex_in_comment.name)
        assert issues == []

    def test_hex_reasoned_token_exempt_honored(self):
        """/* token-exempt: <reason> */ suppresses hex findings too (#444 B3).
        The old exact-string check only honored the bare /* token-exempt */."""
        issues = dtu.check_hardcoded_hex_colors(
            "<div style={{ color: '#ff0000' /* token-exempt: brand red */ }} />",
            "x.jsx",
        )
        assert issues == []

    def test_hex_white_and_black_exempt(self, tmp_path):
        """#fff and #000 should be exempt (too common)."""
        content = textwrap.dedent("""\
            <div style={{ color: '#fff', bg: '#000' }}>
              Test
            </div>
        """)
        p = tmp_path / "test.jsx"
        p.write_text(content, encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, "test.jsx")
        assert issues == []


# ---------------------------------------------------------------------------
# Tests for check_hardcoded_px_values
# ---------------------------------------------------------------------------

class TestPxValues:
    def test_clean_file(self, jsx_file_clean):
        content = jsx_file_clean.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_px_values(content, jsx_file_clean.name)
        assert issues == []

    def test_px_violation(self, jsx_file_px_violation):
        content = jsx_file_px_violation.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_px_values(content, jsx_file_px_violation.name)
        assert len(issues) >= 1
        # At least one violation for 14px or 20px
        px_values = [issue["px"] for issue in issues]
        assert any(px in px_values for px in ["14px", "20px"])

    def test_px_small_exempt(self, jsx_file_px_small):
        """1px and 2px should be exempt (borders/hairlines)."""
        content = jsx_file_px_small.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_px_values(content, jsx_file_px_small.name)
        assert issues == []

    def test_no_style_attr(self, tmp_path):
        """Lines without style= or style =should be skipped."""
        content = textwrap.dedent("""\
            <div className="container">
              fontSize: '14px'
            </div>
        """)
        p = tmp_path / "test.jsx"
        p.write_text(content, encoding="utf-8")
        issues = dtu.check_hardcoded_px_values(content, "test.jsx")
        # Should not flag since no style= present
        assert issues == []

    # --- #444 Phase 1 B3: non-px units must not be misread as px ---
    def test_percent_not_flagged_as_px(self):
        """width: '100%' must NOT be reported as 100px (the regex used to drop
        the unit and treat the bare number as px)."""
        issues = dtu.check_hardcoded_px_values(
            "<div style={{ width: '100%', maxWidth: '60px' }} />", "x.jsx"
        )
        pxes = [i["px"] for i in issues]
        assert "100px" not in pxes
        assert pxes == ["60px"]

    def test_other_css_units_not_flagged(self):
        """vh/vw/em/rem/fr/ms are legitimate non-px units, never flagged."""
        for val in ("100vh", "50vw", "2em", "300ms", "1fr"):
            issues = dtu.check_hardcoded_px_values(
                f"<div style={{{{ x: '{val}' }}}} />", "x.jsx"
            )
            assert issues == [], f"{val} should not be flagged as px"

    def test_unitless_number_still_px(self):
        """React maps a unitless numeric style value to px, so it IS a finding."""
        issues = dtu.check_hardcoded_px_values(
            "<div style={{ fontSize: 24 }} />", "x.jsx"
        )
        assert any(i["px"] == "24px" for i in issues)

    def test_px_reasoned_token_exempt_honored(self):
        """/* token-exempt: <reason> */ suppresses px findings (#444 B3)."""
        issues = dtu.check_hardcoded_px_values(
            "<div style={{ maxWidth: '900px' /* token-exempt: page width */ }} />",
            "x.jsx",
        )
        assert issues == []


# ---------------------------------------------------------------------------
# Tests for scan_jsx_files and exit logic
# ---------------------------------------------------------------------------

class TestScanResults:
    def test_scan_jsx_files_returns_tuple(self):
        """scan_jsx_files returns (hex_issues_dict, px_issues_dict)."""
        hex_issues, px_issues = dtu.scan_jsx_files()
        assert isinstance(hex_issues, dict)
        assert isinstance(px_issues, dict)

    def test_direct_function_hex_detection(self, jsx_file_hex_violation):
        """Direct test of hex detection logic."""
        content = jsx_file_hex_violation.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_hex_colors(content, "test.jsx")
        assert len(issues) > 0
        assert any("#64748b" == i["hex"] for i in issues)

    def test_direct_function_px_detection(self, jsx_file_px_violation):
        """Direct test of px detection logic."""
        content = jsx_file_px_violation.read_text(encoding="utf-8")
        issues = dtu.check_hardcoded_px_values(content, "test.jsx")
        assert len(issues) > 0
        px_vals = [i["px"] for i in issues]
        assert any(v in ["14px", "20px"] for v in px_vals)


class TestCLISubprocess:
    def test_cli_subprocess_with_ci_flag(self):
        """Test running the script via subprocess with --ci flag.

        v2.8.0 lint-policy refactor: --full-scan needed because default is
        now diff-only (lint-policy.md §3 (b) class), and the pytest CI
        environment may not have a resolvable origin/main ref → exit 2.
        """
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage",
             "--full-scan", "--ci"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # The script will scan real project files. If there are violations,
        # exit code should be 1; otherwise 0.
        assert result.returncode in (0, 1)

    def test_cli_subprocess_without_ci_flag(self):
        """Test running the script via subprocess without --ci flag."""
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage",
             "--full-scan"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # Without --ci, should always exit 0 (display-only mode)
        assert result.returncode == 0

    def test_cli_output_contains_violations_info(self):
        """Verify script outputs violation information when violations exist."""
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage",
             "--full-scan"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # Output should contain helpful message or "TOTAL" summary
        assert "✓" in result.stdout or "TOTAL:" in result.stdout or "violation" in result.stdout.lower()


# ---------------------------------------------------------------------------
# Default-path drift guard (#444 Phase 0 keystone)
# ---------------------------------------------------------------------------
# The fixture-based tests above feed inline JSX content, so they never exercise
# the *production default* roots. That blind spot let JSX_TOOLS_DIR / WIZARD_DIR
# sit stale at the pre-TRK-242 docs/ layout after portal source moved to
# tools/portal/src/ — the gate scanned ZERO files and passed vacuously (PR #722
# fixed the paths). These assert the module defaults resolve to a real directory
# that actually contains .jsx, closing that drift class.
class TestDefaultRootsResolve:
    def test_jsx_tools_dir_default_exists_and_is_dir(self):
        assert dtu.JSX_TOOLS_DIR.is_dir(), (
            f"JSX_TOOLS_DIR default does not resolve to a directory: "
            f"{dtu.JSX_TOOLS_DIR}. If portal source moved, update the module "
            f"default (this is exactly the #444 drift the gate went blind on)."
        )

    def test_jsx_tools_dir_default_contains_jsx(self):
        jsx = list(dtu.JSX_TOOLS_DIR.rglob("*.jsx"))
        assert jsx, (
            f"JSX_TOOLS_DIR default resolves but holds no .jsx files: "
            f"{dtu.JSX_TOOLS_DIR}. A gate that scans an empty tree passes "
            f"vacuously — the #444 failure mode. Point it at the real source."
        )

    def test_wizard_dir_default_exists(self):
        assert dtu.WIZARD_DIR.is_dir(), (
            f"WIZARD_DIR default does not resolve: {dtu.WIZARD_DIR}"
        )

    def test_design_tokens_css_default_exists(self):
        assert dtu.DESIGN_TOKENS.is_file(), (
            f"DESIGN_TOKENS default does not resolve: {dtu.DESIGN_TOKENS}"
        )

    def test_default_root_holds_jsx_so_gate_is_not_vacuous(self):
        """Behavioural backstop without touching git: if the production default
        root is empty, the gate passes vacuously (the #444 failure). Assert the
        tree the scanner WOULD walk is non-empty. (The scan_jsx_files() return
        contract is already covered by TestScanResults; we deliberately do NOT
        call it here — it resolves a diff base and fails in shallow CI checkouts
        that lack origin/main.)"""
        assert list(dtu.JSX_TOOLS_DIR.rglob("*.jsx")), (
            f"scanner root {dtu.JSX_TOOLS_DIR} is empty — gate would pass "
            f"vacuously (the #444 drift)."
        )
