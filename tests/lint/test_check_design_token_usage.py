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

    def test_hex_in_block_comment_continuation_not_flagged(self):
        """#444 B4: a hex-shaped token inside a /* */ block comment — including
        a continuation line that does NOT start with '*' — must not be flagged.
        Real example: an issue ref like (extracted in PR #153)."""
        content = (
            "/* ErrorBoundary notes\n"
            "   extracted from foo.jsx in PR #153 (see also #160)\n"
            "*/\n"
            "const s = { color: '#abcdef' };\n"
        )
        issues = dtu.check_hardcoded_hex_colors(content, "x.jsx")
        # Only the real #abcdef survives; #153 / #160 in the comment are gone.
        assert [i["hex"] for i in issues] == ["#abcdef"]

    def test_hex_in_yaml_frontmatter_not_flagged(self):
        """#444 B4: jsx-loader frontmatter prose carries issue refs (PR #158)
        that are hex-shaped but not colors. The leading --- ... --- block is
        stripped before scanning."""
        content = (
            "---\n"
            "title: TenantCard\n"
            "purpose: |\n"
            "  Deferred in PR #158; scaffolded by tool (PR #160).\n"
            "---\n"
            "const s = { color: '#abcdef' };\n"
        )
        issues = dtu.check_hardcoded_hex_colors(content, "x.jsx")
        assert [i["hex"] for i in issues] == ["#abcdef"]

    def test_hex_trailing_block_comment_keeps_real_violation(self):
        """A real hex on the same line as a trailing /* */ comment is still
        caught (blanking, not whole-line skipping)."""
        issues = dtu.check_hardcoded_hex_colors(
            "const s = { color: '#abcdef' }; /* see #153 */", "x.jsx"
        )
        assert [i["hex"] for i in issues] == ["#abcdef"]

    def test_url_scheme_not_treated_as_line_comment(self):
        """#444 B4 self-review catch: the // in https:// must NOT be read as a
        line comment — doing so blanks the rest of the line and would hide a
        real violation after the URL. Found by adversarial self-review."""
        # URL followed by a real hex on the same line: hex must still report.
        issues = dtu.check_hardcoded_hex_colors(
            "const u = 'https://x.com'; const c = '#abcdef';", "x.jsx"
        )
        assert [i["hex"] for i in issues] == ["#abcdef"]
        # A genuine // comment AFTER a URL string still suppresses.
        issues2 = dtu.check_hardcoded_hex_colors(
            "const u = 'https://x.com'; // note #ff0000", "x.jsx"
        )
        assert issues2 == []

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

    def test_px_in_block_comment_not_flagged(self):
        """#444 B4: a px value mentioned inside a block comment is not a real
        style and must not be flagged."""
        content = (
            "/* layout note: was 48px before redesign */\n"
            "<div style={{ fontSize: '14px' }} />\n"
        )
        issues = dtu.check_hardcoded_px_values(content, "x.jsx")
        assert [i["px"] for i in issues] == ["14px"]


# ---------------------------------------------------------------------------
# Tests for check_saturated_token_as_text (#885/#904 stroke-as-text guard)
# ---------------------------------------------------------------------------

class TestSaturatedTokenAsText:
    def test_bare_error_token_as_text_flagged(self):
        issues = dtu.check_saturated_token_as_text(
            '<p className="text-xs text-[color:var(--da-color-error)]">bad</p>', "x.jsx"
        )
        assert len(issues) == 1
        assert issues[0]["token"] == "--da-color-error"
        assert issues[0]["suggestion"] == "--da-color-error-text"
        assert issues[0]["line"] == 1

    def test_warning_token_as_text_flagged(self):
        issues = dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-warning)]">bad</p>', "x.jsx"
        )
        assert [i["suggestion"] for i in issues] == ["--da-color-warning-text"]

    def test_text_variant_is_the_fix_not_flagged(self):
        """The AA -text variant is the FIX — must never be flagged (else the lint
        would reject its own remediation; the trailing \\) anchors the bare token)."""
        assert dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-error-text)]">ok</p>', "x.jsx"
        ) == []
        assert dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-warning-text)]">ok</p>', "x.jsx"
        ) == []

    def test_border_and_bg_not_flagged(self):
        """Saturated token is CORRECT for strokes/borders/backgrounds — only the
        text- utility is the violation."""
        assert dtu.check_saturated_token_as_text(
            '<p className="border-l-2 border-[color:var(--da-color-error)] '
            'bg-[color:var(--da-color-error-soft)]">ok</p>', "x.jsx"
        ) == []

    def test_info_and_success_not_flagged(self):
        """info (#2563eb ~5.2:1) / success (#047857 ~5.5:1) already pass AA as text
        on white and have NO -text variant — out of scope for this rule."""
        assert dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-info)] '
            'text-[color:var(--da-color-success)]">ok</p>', "x.jsx"
        ) == []

    def test_real_904_shape_border_kept_text_swapped(self):
        """The exact #904 line shape: border stays saturated, text uses -text →
        zero findings; the pre-fix form (bare text- token) → exactly one."""
        fixed = ('<p className="border-l-2 border-[color:var(--da-color-error)] '
                 'text-[color:var(--da-color-error-text)]">fixed</p>')
        assert dtu.check_saturated_token_as_text(fixed, "x.jsx") == []
        broken = ('<p className="border-l-2 border-[color:var(--da-color-error)] '
                  'text-[color:var(--da-color-error)]">broken</p>')
        assert len(dtu.check_saturated_token_as_text(broken, "x.jsx")) == 1

    def test_token_exempt_marker_honored(self):
        """A documented exemption (e.g. genuinely on a dark background where the
        saturated token passes) suppresses the finding, like the other checks."""
        assert dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-error)]"> '
            '/* token-exempt: rendered on dark hero bg */ </p>', "x.jsx"
        ) == []

    def test_pattern_inside_comment_not_flagged(self):
        """The bad pattern quoted in a // or /* */ comment is not real code."""
        assert dtu.check_saturated_token_as_text(
            '// avoid text-[color:var(--da-color-error)] here — use the -text variant',
            "x.jsx",
        ) == []

    # --- (c2) inline / style-object FOREGROUND form (className regex's blind spot) ---

    def test_inline_color_error_flagged(self):
        """A bare saturated token on the `color:` inline foreground key IS flagged —
        this is the className regex's blind spot the extension closes."""
        issues = dtu.check_saturated_token_as_text(
            "<span style={{ color: 'var(--da-color-error)' }}>bad</span>", "x.jsx"
        )
        assert len(issues) == 1
        assert issues[0]["token"] == "--da-color-error"
        assert issues[0]["suggestion"] == "--da-color-error-text"
        assert issues[0]["line"] == 1

    def test_colors_map_text_entry_warning_flagged(self):
        """The `text:` key of a colors-map (TIER_COLORS/GROUP_COLORS convention),
        applied downstream as `color: c.text`, IS flagged."""
        issues = dtu.check_saturated_token_as_text(
            "  B: { bg: 'var(--da-color-warning-soft)', "
            "text: 'var(--da-color-warning)', label: 'Partial' },",
            "x.jsx",
        )
        assert [i["suggestion"] for i in issues] == ["--da-color-warning-text"]

    def test_textcolor_alias_key_flagged(self):
        """`textColor:` (camelCase foreground alias) is in the allow-list."""
        issues = dtu.check_saturated_token_as_text(
            "const s = { textColor: 'var(--da-color-error)' };", "x.jsx"
        )
        assert [i["suggestion"] for i in issues] == ["--da-color-error-text"]

    def test_inline_background_border_stroke_not_flagged(self):
        """A saturated hue is CORRECT for backgrounds / borders / strokes / fills —
        these foreground-key look-alikes must NOT be flagged."""
        for prop in (
            "background", "backgroundColor", "bg",
            "border", "borderColor", "stroke", "fill",
        ):
            src = f"const s = {{ {prop}: 'var(--da-color-error)' }};"
            assert dtu.check_saturated_token_as_text(src, "x.jsx") == [], (
                f"{prop} must not be flagged (saturated is correct for it)"
            )

    def test_inline_text_and_soft_variants_not_flagged(self):
        """The `-text` variant (the fix) and the `-soft` background on a foreground
        key are both correct — never flagged (the trailing \\) anchors the bare token)."""
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-error-text)' };", "x.jsx"
        ) == []
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-warning-text)' };", "x.jsx"
        ) == []
        # A -soft token would be an odd choice for text but must still not match
        # the bare-token anchor (it is not the error/warning token this rule targets).
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-error-soft)' };", "x.jsx"
        ) == []

    def test_inline_info_success_not_flagged(self):
        """info/success as an inline foreground already pass AA and have no -text
        variant — out of scope for this rule."""
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-info)' };", "x.jsx"
        ) == []
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-success)' };", "x.jsx"
        ) == []

    def test_inline_token_exempt_marker_honored(self):
        """`/* token-exempt */` suppresses the inline foreground finding too."""
        assert dtu.check_saturated_token_as_text(
            "const s = { color: 'var(--da-color-error)' }; "
            "/* token-exempt: rendered on dark hero bg */",
            "x.jsx",
        ) == []

    def test_inline_pattern_inside_comment_not_flagged(self):
        """The inline bad pattern quoted in a // or /* */ comment is not real code."""
        assert dtu.check_saturated_token_as_text(
            "// e.g. color: 'var(--da-color-error)' — use the -text variant instead",
            "x.jsx",
        ) == []
        assert dtu.check_saturated_token_as_text(
            "/* legacy: color: 'var(--da-color-warning)' */\nconst ok = true;",
            "x.jsx",
        ) == []

    def test_className_form_not_double_counted_by_inline(self):
        """A line carrying only the (c1) className form yields exactly ONE finding —
        the inline regex must not also match it (its `color:var` has no quote)."""
        issues = dtu.check_saturated_token_as_text(
            '<p className="text-[color:var(--da-color-error)]">bad</p>', "x.jsx"
        )
        assert len(issues) == 1
        assert issues[0]["suggestion"] == "--da-color-error-text"

    # --- (c2) TERNARY / CONDITIONAL foreground coverage (the #1074 widening) ---
    # The prior inline regex required a quote IMMEDIATELY after `color:`, so a bare
    # saturated token inside a ternary/conditional VALUE slipped through. The value
    # scan now catches a bare `var(--da-color-(error|warning))` anywhere in the
    # foreground property's value, bounded to that property (stops at the next
    # ,/;/}/newline) so a sibling non-foreground property is never mis-attributed.

    def test_ternary_foreground_bare_error_flagged(self):
        """Real #1074 regression shape (multi-tenant-comparison): a bare saturated
        token in a ternary foreground value IS flagged — the widening's core job."""
        issues = dtu.check_saturated_token_as_text(
            "  const s = { color: isOutlier ? 'var(--da-color-error)' "
            ": 'var(--da-color-tag-fg)' };",
            "x.jsx",
        )
        assert [i["suggestion"] for i in issues] == ["--da-color-error-text"]

    def test_nested_ternary_foreground_bare_flagged(self):
        """Real #1074 regression shape (notification nesting): the bare true-branch
        of a nested `over ? … : near ? … : …` foreground ternary is flagged."""
        issues = dtu.check_saturated_token_as_text(
            "  color: isOverLimit ? 'var(--da-color-error)' "
            ": isNearLimit ? 'var(--da-color-warning)' "
            ": 'var(--da-color-success)',",
            "x.jsx",
        )
        # First bare saturated branch flags the property (one finding per property).
        assert len(issues) == 1
        assert issues[0]["suggestion"] == "--da-color-error-text"

    def test_trap1_sibling_nonfg_property_not_misattributed(self):
        """TRAP 1 (the critical false-positive + anti-tautology test). A saturated
        token on a SIBLING non-foreground property on the SAME line must NOT be
        attributed to the foreground key. BOTH orderings must be clean; the
        `background`-AFTER-`color` case is load-bearing — it FALSE-POSITIVES the
        moment the value-bounding char class is relaxed to `.`, so this test fails
        if the bound is removed."""
        # background BEFORE color: error token precedes the foreground key.
        assert dtu.check_saturated_token_as_text(
            "{ background: 'var(--da-color-error)', color: 'var(--da-color-tag-fg)' }",
            "x.jsx",
        ) == []
        # background AFTER color: error token follows the key — only value-bounding
        # (stop at the comma) keeps `color:` from swallowing the sibling's token.
        assert dtu.check_saturated_token_as_text(
            "{ color: 'var(--da-color-tag-fg)', background: 'var(--da-color-error)' }",
            "x.jsx",
        ) == []

    def test_trap2_ternary_both_text_variants_not_flagged(self):
        """TRAP 2. A ternary whose BOTH branches are AA `-text` variants is the fix,
        not a violation — the trailing `\\)` bare-token anchor keeps `-text` out."""
        assert dtu.check_saturated_token_as_text(
            "color: cond ? 'var(--da-color-error-text)' "
            ": 'var(--da-color-warning-text)',",
            "x.jsx",
        ) == []

    def test_trap2_mixed_ternary_bare_true_branch_flagged(self):
        """TRAP 2 (other half). A MIXED ternary — bare saturated true-branch, AA
        `-text` false-branch — IS a real failure and must flag on the bare branch."""
        issues = dtu.check_saturated_token_as_text(
            "color: cond ? 'var(--da-color-error)' : 'var(--da-color-error-text)',",
            "x.jsx",
        )
        assert [i["suggestion"] for i in issues] == ["--da-color-error-text"]

    def test_trap3_classname_map_value_string_not_double_counted(self):
        """TRAP 3 (map-value variant). A colors-map entry whose VALUE is a Tailwind
        class STRING — `text: 'text-[color:var(--da-color-error)]'` or
        `color: 'text-[…] bg-[…]'` (config-lint SEVERITY_COLORS / summary chips) —
        is counted EXACTLY ONCE by (c1). The `(?<!color:)` guard stops the widened
        inline regex from re-matching the inner `color:var(` and double-flagging."""
        one = dtu.check_saturated_token_as_text(
            "    text: 'text-[color:var(--da-color-error)]',", "x.jsx"
        )
        assert len(one) == 1
        assert one[0]["suggestion"] == "--da-color-error-text"
        two = dtu.check_saturated_token_as_text(
            "color: 'text-[color:var(--da-color-error)] "
            "bg-[color:var(--da-color-error-soft)]',",
            "x.jsx",
        )
        assert len(two) == 1
        assert two[0]["suggestion"] == "--da-color-error-text"

    def test_trap4_nonfg_key_ternary_not_flagged(self):
        """TRAP 4. Non-foreground keys carrying a saturated token in a ternary are
        still correct (saturated hue belongs on paint/stroke/background) — widening
        the value scan must not break the case-sensitive / capital-C key exclusion."""
        assert dtu.check_saturated_token_as_text(
            "background: cond ? 'var(--da-color-error)' : 'var(--da-color-success)',",
            "x.jsx",
        ) == []
        assert dtu.check_saturated_token_as_text(
            "borderColor: cond ? 'var(--da-color-error)' : 'var(--da-color-warning)',",
            "x.jsx",
        ) == []

    def test_trap4_hyphenated_css_key_not_misattributed(self):
        """TRAP 4 (hyphenated variant). The `(?<![\\w-])` key prefix stops the
        `color` alternative from matching mid-identifier — a `-color` SUFFIX of a
        hyphenated CSS property (`background-color:` / `border-color:` / a custom
        `--brand-color:`), which the plain `\\b` anchor let through. These are NOT
        foreground text and must stay clean; a standalone `color:` at a real boundary
        still flags. Fails if the anchor regresses to `\\b`."""
        for hyphenated in (
            "background-color: 'var(--da-color-error)',",
            "border-color: cond ? 'var(--da-color-error)' : 'var(--da-color-tag-fg)',",
            "--brand-color: 'var(--da-color-warning)',",
        ):
            assert dtu.check_saturated_token_as_text(hyphenated, "x.jsx") == [], hyphenated
        # standalone foreground `color:` at a `{`/space/comma boundary still flags.
        assert [i["suggestion"] for i in dtu.check_saturated_token_as_text(
            "{ color: 'var(--da-color-error)' }", "x.jsx"
        )] == ["--da-color-error-text"]


# ---------------------------------------------------------------------------
# Tests for scan_jsx_files and exit logic
# ---------------------------------------------------------------------------

class TestScanResults:
    def test_scan_jsx_files_returns_tuple(self):
        """scan_jsx_files returns (hex, px, token) issue dicts."""
        hex_issues, px_issues, token_issues = dtu.scan_jsx_files()
        assert isinstance(hex_issues, dict)
        assert isinstance(px_issues, dict)
        assert isinstance(token_issues, dict)

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
