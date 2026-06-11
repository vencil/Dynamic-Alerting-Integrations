"""Tests for check_window_x_no_fallback.py — forbid module-scope no-fallback window-global reads (dev-rules.md §S6).

Pinned contracts
----------------
1. **Detection** — three rule classes:
   - `global-read`: `^const X = window.__Y;` / `globalThis` variant
   - `react-destructure`: `^const { useState } = React;`
   - `global-destructure`: `^const { a, b } = window.__Y;` incl. the
     MULTI-LINE form. Regression pin: the three self-service-portal
     Tab modules destructured `window.__portalShared` at module scope
     across 4 lines; the original single-identifier regex missed them
     and the committed bundle threw TypeError at load time while every
     smoke check stayed green.
2. **Allowed**:
   - fallback form `const t = window.__t || ((zh, en) => en);`
   - destructure with fallback `const { a } = window.__X || {};`
   - function-scope reads (indented — regexes anchor at column 0)
   - ESM imports
3. **Suppression**: `<!-- window-x-no-fallback: ignore -->` within
   3-line lookback; frontmatter `--- ... ---` stripped (line numbers
   preserved).
4. **Live dogfood**: default scan roots → 0 findings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_window_x_no_fallback as lint  # noqa: E402

import pytest  # noqa: E402

FAKE = Path("fake.jsx")


def _kinds(src: str):
    return [(kind, line_no) for line_no, kind, _ in lint.find_violations(src, FAKE)]


# ---------------------------------------------------------------------------
# Detection — global-read (pattern A)
# ---------------------------------------------------------------------------
class TestGlobalRead:
    @pytest.mark.parametrize(
        "src",
        [
            "const X = window.__portalShared;\n",
            "const X = globalThis.__portalShared;\n",
            "const engine = window.__alertEngine;\n",
        ],
    )
    def test_flagged(self, src):
        assert _kinds(src) == [("global-read", 1)]

    def test_fallback_form_allowed(self):
        assert _kinds("const t = window.__t || ((zh, en) => en);\n") == []

    def test_function_scope_allowed(self):
        assert _kinds("  const X = window.__X;\n") == []


# ---------------------------------------------------------------------------
# Detection — global-destructure (pattern C; the portal-shared regression)
# ---------------------------------------------------------------------------
class TestGlobalDestructure:
    def test_single_line_flagged(self):
        src = "const { a, b } = window.__portalShared;\n"
        assert _kinds(src) == [("global-destructure", 1)]

    def test_multi_line_flagged(self):
        # Exact shape of the AlertPreviewTab regression.
        src = (
            "const {\n"
            "  RULE_PACK_DATA, generateSampleYaml, parseYaml, simulateAlerts,\n"
            "  RulePackSelector,\n"
            "} = window.__portalShared;\n"
        )
        assert _kinds(src) == [("global-destructure", 1)]

    def test_globalthis_variant_flagged(self):
        src = "const { a } = globalThis.__shared;\n"
        assert _kinds(src) == [("global-destructure", 1)]

    def test_destructure_with_fallback_allowed(self):
        assert _kinds("const { a } = window.__X || {};\n") == []

    def test_function_scope_allowed(self):
        assert _kinds("  const { a } = window.__X;\n") == []

    def test_react_destructure_is_not_double_counted(self):
        # `= React;` matches pattern B only, not pattern C.
        src = "const { useState } = React;\n"
        assert _kinds(src) == [("react-destructure", 1)]


# ---------------------------------------------------------------------------
# Suppression + frontmatter
# ---------------------------------------------------------------------------
class TestSuppression:
    def test_escape_marker_lookback(self):
        src = (
            "// <!-- window-x-no-fallback: ignore -->\n"
            "const { a } = window.__X;\n"
        )
        assert _kinds(src) == []

    def test_frontmatter_stripped_line_numbers_preserved(self):
        src = (
            "---\n"
            "title: x\n"
            "---\n"
            "const { a } = window.__X;\n"
        )
        assert _kinds(src) == [("global-destructure", 4)]


# ---------------------------------------------------------------------------
# Live dogfood — repo must be clean after the Tab ESM migration
# ---------------------------------------------------------------------------
class TestLiveRepo:
    def test_default_scan_clean(self):
        findings = lint.scan(lint.collect_default_paths())
        assert findings == [], (
            "dev-rules §S6 violations in repo: "
            + "; ".join(f"{p}:{n} {s}" for p, n, _, s in findings)
        )
