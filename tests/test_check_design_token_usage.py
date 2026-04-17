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
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
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
        """Test running the script via subprocess with --ci flag."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage",
             "--ci"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # The script will scan real project files. If there are violations,
        # exit code should be 1; otherwise 0.
        assert result.returncode in (0, 1)

    def test_cli_subprocess_without_ci_flag(self):
        """Test running the script via subprocess without --ci flag."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # Without --ci, should always exit 0 (display-only mode)
        assert result.returncode == 0

    def test_cli_output_contains_violations_info(self):
        """Verify script outputs violation information when violations exist."""
        result = subprocess.run(
            [sys.executable, "-m", "scripts.tools.lint.check_design_token_usage"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # Output should contain helpful message or "TOTAL" summary
        assert "✓" in result.stdout or "TOTAL:" in result.stdout or "violation" in result.stdout.lower()
