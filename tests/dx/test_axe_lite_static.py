"""Tests for axe_lite_static.py — static WCAG heuristic scanner for JSX.

Audit flagged 0% coverage. The 4 scan functions are pure (string in,
findings out), so testing is straightforward — we hand-craft minimal
JSX snippets that hit each branch.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import axe_lite_static as axe  # noqa: E402


# ---------------------------------------------------------------------------
# strip_frontmatter — pure helper
# ---------------------------------------------------------------------------
class TestStripFrontmatter:
    def test_no_frontmatter_returns_unchanged(self):
        src = "function Foo() {}"
        assert axe.strip_frontmatter(src) == src

    def test_yaml_frontmatter_stripped(self):
        src = "---\ntitle: x\n---\nfunction Foo() {}"
        out = axe.strip_frontmatter(src)
        assert "title" not in out
        assert "function Foo()" in out

    def test_unterminated_frontmatter_returned_unchanged(self):
        # Opens with `---` but no closing line — defensive: no slice.
        src = "---\ntitle: x\nstill in frontmatter"
        assert axe.strip_frontmatter(src) == src


# ---------------------------------------------------------------------------
# scan_unicode_status — WCAG 1.4.1
# ---------------------------------------------------------------------------
# NOTE: PR #290 surfaced that the original backward-walk gave up at the
# first `>`, making the flag path unreachable for typical JSX. This bug
# is now fixed (this PR); the tests below pin the corrected behaviour.
class TestScanUnicodeStatus:
    def test_empty_input_no_findings(self):
        assert axe.scan_unicode_status("") == []

    def test_no_status_chars_no_findings(self):
        assert axe.scan_unicode_status("function f() { return 1; }") == []

    def test_symbol_inside_attribute_value_ignored(self):
        # Symbol is INSIDE the opening tag's attribute string — the
        # `close_gt > i` branch returns early without flagging.
        src = '<input title="✓ done" />'
        assert axe.scan_unicode_status(src) == []

    def test_aria_hidden_passes(self):
        # aria-hidden on the wrapping element silences the warning.
        src = '<span aria-hidden="true">✓</span>'
        assert axe.scan_unicode_status(src) == []

    def test_aria_label_passes(self):
        src = '<span aria-label="success">✓</span>'
        assert axe.scan_unicode_status(src) == []

    def test_aria_labelledby_passes(self):
        src = '<span aria-labelledby="status-msg">✓</span>'
        assert axe.scan_unicode_status(src) == []

    def test_bare_symbol_in_div_flagged(self):
        # Post-fix: backward walk now finds the wrapping <div> and flags
        # because no aria-* attribute is present.
        src = '<div>⚠ warning</div>'
        findings = axe.scan_unicode_status(src)
        assert len(findings) == 1
        line, msg = findings[0]
        assert "status symbol" in msg
        assert "aria-hidden" in msg or "aria-label" in msg

    def test_multiple_bare_symbols_each_flagged(self):
        src = '<div>✓</div>\n<section>❌</section>'
        findings = axe.scan_unicode_status(src)
        assert len(findings) == 2

    def test_walks_past_closing_sibling_to_find_real_wrapper(self):
        # The fix: walking backward from ✓, we encounter `</span>` first.
        # Pre-fix this was treated as "give up at >". Post-fix we walk
        # PAST the closing sibling to find <div>, the real wrapper.
        src = '<div><span>x</span>✓</div>'
        findings = axe.scan_unicode_status(src)
        assert len(findings) == 1

    def test_parent_aria_label_within_window_passes(self):
        # Wrapper has no aria-* but parent within 500-char window does.
        src = '<div aria-label="status"><span>✓</span></div>'
        assert axe.scan_unicode_status(src) == []

    def test_returns_list_type(self):
        # Smoke: function always returns list (callable contract).
        result = axe.scan_unicode_status('<a>x</a>')
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# scan_buttons_without_name — WCAG 4.1.2
# ---------------------------------------------------------------------------
class TestScanButtonsWithoutName:
    def test_button_with_text_passes(self):
        src = '<button>Click me</button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_button_with_aria_label_passes(self):
        src = '<button aria-label="close" onClick={x}></button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_button_with_title_passes(self):
        src = '<button title="close"></button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_empty_button_flagged(self):
        src = '<button></button>'
        findings = axe.scan_buttons_without_name(src)
        assert len(findings) == 1
        assert "no accessible name" in findings[0][1]

    def test_jsx_expression_inside_treated_as_accessible(self):
        # i18n pattern: button has {t('Submit')} — runtime string =
        # accept (heuristic to reduce false positives).
        src = '<button>{t("Submit", "送出")}</button>'
        assert axe.scan_buttons_without_name(src) == []

    def test_aria_hidden_only_child_does_not_count(self):
        # icon button: only child is aria-hidden span → no accessible name.
        src = '<button><span aria-hidden="true">✓</span></button>'
        findings = axe.scan_buttons_without_name(src)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# scan_unlabeled_inputs — WCAG 3.3.2
# ---------------------------------------------------------------------------
class TestScanUnlabeledInputs:
    def test_input_with_placeholder_passes(self):
        src = '<input type="text" placeholder="search" />'
        assert axe.scan_unlabeled_inputs(src) == []

    def test_input_with_aria_label_passes(self):
        src = '<input type="text" aria-label="name" />'
        assert axe.scan_unlabeled_inputs(src) == []

    def test_textarea_with_title_passes(self):
        src = '<textarea title="comments" />'
        assert axe.scan_unlabeled_inputs(src) == []

    def test_unlabeled_input_flagged(self):
        src = '<input type="text" />'
        findings = axe.scan_unlabeled_inputs(src)
        assert len(findings) == 1
        assert "input" in findings[0][1]

    def test_hidden_submit_button_radio_skipped(self):
        # Non-text input types skip the label check.
        src = (
            '<input type="hidden" />\n'
            '<input type="submit" />\n'
            '<input type="checkbox" />\n'
            '<input type="radio" />\n'
        )
        assert axe.scan_unlabeled_inputs(src) == []

    def test_label_htmlfor_id_match_passes(self):
        # Sibling <label htmlFor="name"> labels <input id="name">.
        src = '<label htmlFor="name">Name</label><input type="text" id="name" />'
        assert axe.scan_unlabeled_inputs(src) == []

    def test_label_htmlfor_template_form_passes(self):
        # Templated id: <label htmlFor={`name${suffix}`}>. The scanner's
        # template-form regex looks for the input's id appearing inside
        # the template literal — `name` matches.
        src = '<label htmlFor={`name${suffix}`}>L</label><input type="text" id="name" />'
        assert axe.scan_unlabeled_inputs(src) == []


# ---------------------------------------------------------------------------
# scan_color_only_severity — WCAG 1.4.1 complementary
# ---------------------------------------------------------------------------
class TestScanColorOnlySeverity:
    def test_no_color_token_no_findings(self):
        src = '<div className="text-blue">info</div>'
        assert axe.scan_color_only_severity(src) == []

    def test_color_with_border_passes(self):
        src = '<div className="text-[color:var(--da-color-error)] border-2">err</div>'
        assert axe.scan_color_only_severity(src) == []

    def test_color_with_underline_passes(self):
        src = '<span className="text-[color:var(--da-color-warning)] underline">w</span>'
        assert axe.scan_color_only_severity(src) == []

    def test_color_only_flagged(self):
        src = '<div className="text-[color:var(--da-color-error)]">error msg</div>'
        findings = axe.scan_color_only_severity(src)
        assert len(findings) == 1
        assert "non-color signal" in findings[0][1]

    def test_color_with_unicode_symbol_in_window_passes(self):
        src = (
            '<div>'
            '<span className="text-[color:var(--da-color-error)]">'
            '✓ done</span></div>'
        )
        assert axe.scan_color_only_severity(src) == []

    def test_color_with_role_alert_passes(self):
        src = '<div role="alert" className="text-[color:var(--da-color-error)]">err</div>'
        assert axe.scan_color_only_severity(src) == []


# ---------------------------------------------------------------------------
# check_file + main — file-bound + CLI
# ---------------------------------------------------------------------------
class TestCheckFile:
    def test_clean_file_returns_zero(self, tmp_path, capsys):
        f = tmp_path / "ok.jsx"
        f.write_text(
            'function X() { return <button>OK</button>; }',
            encoding="utf-8",
        )
        total = axe.check_file(f)
        assert total == 0
        out = capsys.readouterr().out
        assert "[ok.jsx]" in out

    def test_file_with_violations_returns_count(self, tmp_path, capsys):
        f = tmp_path / "bad.jsx"
        f.write_text(
            'function X() { return <><button></button><input type="text" /></>; }',
            encoding="utf-8",
        )
        total = axe.check_file(f)
        assert total >= 2  # at least button + input
        out = capsys.readouterr().out
        assert "L1" in out

    def test_truncates_long_lists_with_summary(self, tmp_path, capsys):
        # 12 unlabeled buttons → check_file prints first 10 + "and 2 more".
        f = tmp_path / "many.jsx"
        f.write_text("\n".join(["<button></button>"] * 12), encoding="utf-8")
        axe.check_file(f)
        out = capsys.readouterr().out
        assert "and 2 more" in out


class TestMain:
    def test_no_args_returns_two_with_usage(self, capsys):
        rc = axe.main(["axe_lite_static.py"])
        assert rc == 2
        out = capsys.readouterr().out
        assert "usage:" in out

    def test_help_flag_returns_zero(self, capsys):
        rc = axe.main(["axe_lite_static.py", "--help"])
        assert rc == 0
        assert "usage" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self, capsys):
        assert axe.main(["axe_lite_static.py", "-h"]) == 0

    def test_clean_file_returns_zero(self, tmp_path):
        f = tmp_path / "ok.jsx"
        f.write_text('<button>OK</button>', encoding="utf-8")
        assert axe.main(["axe_lite_static.py", str(f)]) == 0

    def test_violations_return_one(self, tmp_path):
        f = tmp_path / "bad.jsx"
        f.write_text('<button></button>', encoding="utf-8")
        assert axe.main(["axe_lite_static.py", str(f)]) == 1
