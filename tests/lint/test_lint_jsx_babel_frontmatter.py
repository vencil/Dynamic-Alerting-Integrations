"""Unit tests for lint_jsx_babel.py front-matter strip regex (PR-portal-6).

Regression for the bug that bit PR-portal-2 (#204): the front-matter
strip pattern `^---[\\s\\S]*?---\\s*\\n?` matched any 3-dash substring
including the first 3 chars of `------` separator lines inside YAML
`purpose:` blocks. Result: closing `---` "found" early → remaining
front-matter bled into JS code → silent Babel parse error
("Invalid left-hand side in prefix operation") at the next non-blank
line.

Fix anchors closing `---` to its own line:
  `^---\\r?\\n[\\s\\S]*?\\r?\\n---\\s*(?:\\r?\\n|$)`

Same regex shipped in 3 places — `_transform_jsx` here mirrors
`docs/assets/jsx-loader.html` `loadDependency` strip + `renderJSX`
strip; tests here exercise the Python copy as proxy for all three.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

from lint_jsx_babel import _transform_jsx  # noqa: E402


class TestFrontMatterStrip:
    """Verify _transform_jsx removes the FM block but preserves body."""

    def test_simple_frontmatter_stripped(self):
        src = '---\ntitle: x\n---\nconst foo = 1;\n'
        out = _transform_jsx(src)
        assert 'title' not in out
        assert 'const foo = 1;' in out

    def test_purpose_block_with_dash_separator_NOT_consumed(self):
        """The pre-PR-portal-6 regex bit here. ----- inside purpose:
        was matched as the closing ---, leaving the rest of the
        front-matter as code → parse error.
        """
        src = (
            '---\n'
            'title: x\n'
            'purpose: |\n'
            '  Description.\n'
            '\n'
            '  Section header\n'
            '  -----\n'
            '  More text.\n'
            '---\n'
            'const foo = 1;\n'
        )
        out = _transform_jsx(src)
        # Body must be preserved verbatim.
        assert 'const foo = 1;' in out
        # Front-matter prose must NOT leak into code.
        assert 'Description' not in out
        assert 'Section header' not in out
        assert 'More text' not in out

    def test_long_dash_separator_in_purpose(self):
        """Same bug class — 50+ dashes in purpose block also poison
        the old regex.
        """
        src = (
            '---\n'
            'title: x\n'
            'purpose: |\n'
            '  Description.\n'
            '  ----------------------------------------\n'
            '  More.\n'
            '---\n'
            'const foo = 1;\n'
        )
        out = _transform_jsx(src)
        assert 'const foo = 1;' in out
        assert 'Description' not in out
        assert '----------------------------------------' not in out

    def test_frontmatter_with_yaml_dependencies_block(self):
        """Real-world shape — multi-line `dependencies:` array inside
        the FM block.
        """
        src = (
            '---\n'
            'title: My Tool\n'
            'dependencies: [\n'
            '  "_common/hooks/useDebouncedValue.js",\n'
            '  "_common/components/ErrorBoundary.jsx"\n'
            ']\n'
            '---\n'
            'const Tool = () => null;\n'
        )
        out = _transform_jsx(src)
        assert 'const Tool = () => null;' in out
        assert 'dependencies' not in out

    def test_no_frontmatter_passes_through(self):
        src = 'const foo = 1;\n'
        out = _transform_jsx(src)
        assert out == 'const foo = 1;\n'

    def test_crlf_line_endings_handled(self):
        """Files with Windows-style line endings still strip cleanly
        (regex uses `\\r?\\n` to allow either).
        """
        src = '---\r\ntitle: x\r\n---\r\nconst foo = 1;\r\n'
        out = _transform_jsx(src)
        assert 'title' not in out
        assert 'const foo = 1;' in out

    def test_fm_only_no_body_does_not_crash(self):
        """Edge case: file is pure front-matter, no body."""
        src = '---\ntitle: x\n---'
        out = _transform_jsx(src)
        # Body is empty string; assertion is just "doesn't crash".
        assert 'title' not in out
