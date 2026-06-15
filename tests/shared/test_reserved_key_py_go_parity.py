"""Python↔Go reserved-key allowlist parity guard.

The tenant-key validator exists twice and the two MUST agree:
  - Python: scripts/tools/_lib_constants.py — VALID_RESERVED_KEYS / VALID_RESERVED_PREFIXES
  - Go:     components/threshold-exporter/app/pkg/config/types.go — validReservedKeys / validReservedPrefixes

A key present in one side but not the other means a shipped reserved key gets
falsely flagged `unknown reserved key '...' (typo?)` by the lagging side. That
exact drift shipped once — Go gained `_custom_alerts` (#741) but Python lagged —
and nothing caught it: the only nominal gate (scripts/tools/dx/sync_schema.py)
reads `app/config.go` (where the map does NOT live) and only checks the JSON
schema, never the Python set. This test is the real gate; it runs in CI (pytest).
Keep it parser-simple — it mirrors sync_schema.extract_go_keys' regex shape.
"""
import os
import re

import pytest

from _lib_constants import VALID_RESERVED_KEYS, VALID_RESERVED_PREFIXES

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TYPES_GO = os.path.join(
    _REPO_ROOT, "components", "threshold-exporter", "app", "pkg", "config", "types.go")

pytestmark = pytest.mark.skipif(
    not os.path.exists(_TYPES_GO),
    reason="Go source not present (e.g. Python-only checkout)")


def _types_go() -> str:
    with open(_TYPES_GO, encoding="utf-8") as f:
        return f.read()


def _strip_go_line_comments(text: str) -> str:
    """Drop Go `//` line comments. A key deprecated by commenting it out
    (`// "_old": true,`) is GONE for the compiler — the parser must agree, or
    the guard cries false drift (Gemini review). Stripping on the FULL source
    (before the brace-match) also stops a `}` inside a comment from truncating
    the captured block. Safe here: reserved keys/prefixes are `_`-identifiers,
    never contain `//`."""
    return re.sub(r"//.*", "", text)


def _go_reserved_keys(content: str) -> set[str]:
    content = _strip_go_line_comments(content)
    m = re.search(r"var validReservedKeys = map\[string\]bool\{(.+?)\}", content, re.DOTALL)
    assert m, "could not locate validReservedKeys map in types.go"
    return set(re.findall(r'"([^"]+)"\s*:\s*true', m.group(1)))


def _go_reserved_prefixes(content: str) -> set[str]:
    content = _strip_go_line_comments(content)
    m = re.search(r"var validReservedPrefixes = \[\]string\{(.+?)\}", content, re.DOTALL)
    assert m, "could not locate validReservedPrefixes slice in types.go"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


class TestPyGoReservedKeyParity:
    """Python and Go reserved-key allowlists must stay byte-for-byte equivalent."""

    def test_keys_match(self):
        go_keys = _go_reserved_keys(_types_go())
        py_keys = set(VALID_RESERVED_KEYS)
        assert go_keys == py_keys, (
            "Python↔Go reserved-KEY drift — a shipped key will be flagged as a "
            f"typo by the lagging side. py-only={sorted(py_keys - go_keys)}, "
            f"go-only={sorted(go_keys - py_keys)}")

    def test_prefixes_match(self):
        go_prefixes = _go_reserved_prefixes(_types_go())
        py_prefixes = set(VALID_RESERVED_PREFIXES)
        assert go_prefixes == py_prefixes, (
            "Python↔Go reserved-PREFIX drift. "
            f"py-only={sorted(py_prefixes - go_prefixes)}, "
            f"go-only={sorted(go_prefixes - py_prefixes)}")


class TestGoParserRobustness:
    """The Go parser must mirror the compiler's view, not the raw bytes."""

    _SYNTHETIC = (
        'var validReservedKeys = map[string]bool{\n'
        '    "_silent_mode": true,\n'
        '    // "_deprecated_old_key": true,  <- commented out = gone for Go\n'
        '}\n'
        'var validReservedPrefixes = []string{\n'
        '    "_state_",\n'
        '    // "_old_prefix",\n'
        '}\n'
    )

    def test_commented_out_key_is_ignored(self):
        # Without comment-stripping the regex would extract _deprecated_old_key
        # and the parity guard would cry false drift on a deprecate-by-comment.
        assert _go_reserved_keys(self._SYNTHETIC) == {"_silent_mode"}

    def test_commented_out_prefix_is_ignored(self):
        assert _go_reserved_prefixes(self._SYNTHETIC) == {"_state_"}
