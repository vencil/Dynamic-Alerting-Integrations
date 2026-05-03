"""Unit tests for check_hub_badge_drift.py (PR-portal-7).

Lint that detects hardcoded `N Tools` / `N 個工具` counts in the Hub
UI (`docs/interactive/index.html`). The whole point: prevent the
silent drift the user spotted (Reference badge said "2 Tools" while
the rendered card grid had 3 cards).

Tests cover:
  - Live repo passes (post-PR-portal-7 state)
  - Synthetic violations get flagged (HTML body + i18n EN + i18n ZH)
  - {N} placeholder is the legitimate form (NOT flagged)
  - Ignore marker suppresses on the next 3 lines
  - Counts in unrelated i18n strings are NOT flagged
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_hub_badge_drift  # noqa: E402


def _scan(tmp_path, content):
    """Write a synthetic Hub HTML and scan it."""
    f = tmp_path / "index.html"
    f.write_text(content, encoding="utf-8")
    return check_hub_badge_drift.scan_hub(f)


# ---------------------------------------------------------------------------
# Live repo state (post-PR-portal-7 must be clean)
# ---------------------------------------------------------------------------


class TestLiveRepoIsClean:
    def test_real_hub_passes(self):
        """The shipped index.html (after PR-portal-7) must have no
        hardcoded counts. If this test fails, someone re-introduced
        a literal — the regression target.
        """
        violations = check_hub_badge_drift.scan_hub()
        assert violations == [], (
            "Hub regressed to hardcoded counts:\n  "
            + "\n  ".join(violations)
        )


# ---------------------------------------------------------------------------
# Violation detection — synthetic inputs
# ---------------------------------------------------------------------------


class TestStaticHtmlBadgeFlagged:
    def test_en_badge_flagged(self, tmp_path):
        violations = _scan(tmp_path, '<span id="reference-badge">2 Tools</span>\n')
        assert any("static HTML *-badge" in v for v in violations)

    def test_zh_badge_flagged(self, tmp_path):
        violations = _scan(tmp_path, '<span id="reference-badge">2 個工具</span>\n')
        assert any("static HTML *-badge" in v for v in violations)

    def test_with_extra_attributes(self, tmp_path):
        """Real HTML often has extra attributes — class etc. Lint must
        match across the attribute string."""
        violations = _scan(
            tmp_path,
            '<span class="journey-phase-badge reference" id="reference-badge">3 Tools</span>\n',
        )
        assert any("static HTML *-badge" in v for v in violations)


class TestI18nBadgeFlagged:
    def test_en_i18n_value_flagged(self, tmp_path):
        violations = _scan(tmp_path, "      'reference-badge': '2 Tools',\n")
        assert any("i18n *-badge value" in v for v in violations)

    def test_zh_i18n_value_flagged(self, tmp_path):
        violations = _scan(tmp_path, "      'reference-badge': '2 個工具',\n")
        assert any("i18n *-badge value" in v for v in violations)


class TestHeroDescFlagged:
    def test_en_hero_desc_flagged(self, tmp_path):
        violations = _scan(
            tmp_path, "      'hero-desc': 'Explore 32 tools across 5 phases',\n"
        )
        assert any("hero-desc" in v for v in violations)

    def test_zh_hero_desc_flagged(self, tmp_path):
        violations = _scan(
            tmp_path, "      'hero-desc': '探索 32 個按用戶旅程組織的工具',\n"
        )
        assert any("hero-desc" in v for v in violations)


# ---------------------------------------------------------------------------
# Legitimate forms NOT flagged
# ---------------------------------------------------------------------------


class TestPlaceholderForm:
    def test_n_placeholder_html_not_flagged(self, tmp_path):
        violations = _scan(tmp_path, '<span id="reference-badge">{N} Tools</span>\n')
        assert violations == []

    def test_n_placeholder_i18n_not_flagged(self, tmp_path):
        violations = _scan(tmp_path, "      'reference-badge': '{N} Tools',\n")
        assert violations == []

    def test_n_placeholder_hero_not_flagged(self, tmp_path):
        violations = _scan(
            tmp_path, "      'hero-desc': 'Explore {N} tools',\n"
        )
        assert violations == []


class TestUnrelatedCountsNotFlagged:
    def test_count_in_other_i18n_string(self, tmp_path):
        """Counts in non-badge i18n strings (e.g. someone's footnote)
        are NOT this lint's concern."""
        violations = _scan(
            tmp_path, "      'random-key': 'See 5 examples',\n"
        )
        assert violations == []

    def test_count_in_html_body_outside_badge(self, tmp_path):
        violations = _scan(tmp_path, "<p>You have 7 unread messages.</p>\n")
        assert violations == []


# ---------------------------------------------------------------------------
# Ignore marker
# ---------------------------------------------------------------------------


class TestIgnoreMarker:
    def test_marker_on_same_line_block(self, tmp_path):
        """Marker within 3 lines above suppresses the violation."""
        content = (
            "<!-- hub-badge-drift: ignore -->\n"
            '<span id="reference-badge">5 Tools</span>\n'
        )
        violations = _scan(tmp_path, content)
        assert violations == []

    def test_marker_4_lines_above_does_NOT_suppress(self, tmp_path):
        content = (
            "<!-- hub-badge-drift: ignore -->\n"
            "line 2\n"
            "line 3\n"
            "line 4\n"
            '<span id="reference-badge">5 Tools</span>\n'
        )
        violations = _scan(tmp_path, content)
        assert len(violations) == 1
