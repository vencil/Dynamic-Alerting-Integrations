"""Tests for check_codename_leak.py — diff-aware refactor (lint-policy.md compliance).

Pinned contracts
----------------
1. **Pattern detection** (negative regex):
   - "Phase .a/.b/.c" lowercase dot-prefixed → flagged
   - "Phase A/B/C" plain (no dot) → NOT flagged (legitimate user-facing playbook structure)
   - "Track [A-E]" → flagged
   - "TD-NN" / "S#NN" / "HA-NN" / "REG-NN" / "PR-N" / "Wave N" / "[A-E]-NNN" → flagged

2. **False-positive escape** (ALLOW_LINE_SUBSTRINGS):
   - Lines containing SHA-256 / RFC-XXX / ISO-XXXX / CVE-XXXX → entire line skipped

3. **Code-comment skip**:
   - Python `#` line / shell `#` line / Go|JS|TS `//` line → skipped (not user-visible)
   - Markdown HTML comments NOT skipped (visible in raw .md view)

4. **Diff parsing** (_parse_unified_zero_diff in _lint_helpers):
   - `@@ -X,Y +A,B @@` headers parse correctly
   - Only `+` lines returned with correct line numbers
   - `-` lines do NOT advance counter; `+` lines do

5. **Bypass tag** (parse_bypass_tag in _lint_helpers):
   - `bypass-lint: codename-leak` + `reason: ...` matched
   - Wrong lint name in tag → no match
   - Case-insensitive matching
   - Multiple bypass tags → only matching one returned

These tests exercise the lint's individual stages without invoking subprocess
git commands (covered in integration tests when CI runs the full lint).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_codename_leak as ccl  # noqa: E402
from _lint_helpers import (  # noqa: E402
    _parse_unified_zero_diff,
    parse_bypass_tag,
)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------
class TestPatternDetection:
    @pytest.mark.parametrize(
        "line,expected_label_substr",
        [
            ("Some text mentioning Phase .a feature", "Phase .a/.b/.c letter"),
            ("Working in Phase .b track", "Phase .a/.b/.c letter"),
            ("Plan involves Phase .c work", "Phase .a/.b/.c letter"),
            ("Track A items only", "Track A/B/C letter"),
            ("See Wave 3 backlog", "Wave N"),
            ("Issue TD-030 follow-up", "TD-NNN ticket"),
            ("S#74 closure", "S#NN sprint id"),
            ("HA-11 hardening", "HA-NN sprint id"),
            ("Done in PR-2d", "PR-N internal id"),
            ("Item C-12 done", "Letter-prefix planning id"),
            ("Item B-4 status", "Letter-prefix planning id"),
            # v2.8.0 #462: extended patterns
            ("Tracking via DEC-B decision", "DEC-X decision tag"),
            ("Tracked under DEC-F resolution", "DEC-X decision tag"),
            ("Baseline taken on v2.7.0-final", "version -final/-rc/-preview suffix"),
            ("Cut from v2.8.0-rc1 build", "version -final/-rc/-preview suffix"),
            ("Pre-release v3.0.0-alpha noted", "version -final/-rc/-preview suffix"),
            # v2.0.0-preview series existed in this repo; both bare and
            # dotted suffix variants must flag (regex stops at the word
            # boundary after "preview", which is sufficient for line-level
            # leak detection).
            ("Baseline tagged at v2.0.0-preview.4", "version -final/-rc/-preview suffix"),
            ("Try v3.0.0-preview2 candidate", "version -final/-rc/-preview suffix"),
            # B-1 Phase 2 case: B-1 alone is enough to catch the leak.
            ("v2.8.0 B-1 Phase 2 landed", "Letter-prefix planning id"),
        ],
    )
    def test_codename_patterns_flagged(self, line, expected_label_substr):
        hits = ccl.scan_line(line)
        assert hits, f"expected match for: {line!r}"
        assert any(expected_label_substr in label for label, _ in hits)

    @pytest.mark.parametrize(
        "line",
        [
            # User-facing playbook style — must NOT flag
            "Phase A: Triage",
            "Phase B: Convert + Shadow",
            "Phase C: Cutover",
            # Plain text, no codename signature
            "This is normal text without internal markers.",
            "Use the v2.8.0 release notes for details.",
            # Code identifier with letter-digit but not in 1-3 digit range
            "version 2-1-0 release",
            # v2.8.0 #462: plain semver without -final/-rc suffix must NOT flag
            "Released as v2.8.0 on 2026-05-12.",
            "Run tools/v2.7.0 for the prior contract.",
            # "DEC" alone (no -<letter>) must NOT flag (used in DECision prose)
            "DECoration is fine on the wall.",
            # Legit algorithm-phase prose with digit must NOT flag (we dropped
            # the Phase N digit pattern due to high FP rate on these uses).
            "Phase 1: Mtime Guard — Quick Filtering",
            "Journey Phase 0-2 (onboarding=2, operate=1, explore=0)",
            "Phase 2: Per-File Hash Diff",
        ],
    )
    def test_clean_lines_not_flagged(self, line):
        hits = ccl.scan_line(line)
        assert hits == [], f"unexpected match in: {line!r}"


class TestAllowlistEscape:
    @pytest.mark.parametrize(
        "line",
        [
            "computed via SHA-256 hash",
            "described in RFC-7234",
            "compliant with ISO-8601 timestamps",
            "tracking CVE-2024-12345",
            "GHSA-abcd-1234 advisory",
            "encoded UTF-8 / UTF-16 strings",
            "uses HTTP/2 multiplexing",
        ],
    )
    def test_known_technical_acronyms_skip_line(self, line):
        # Even if the line contains a codename-pattern hit, ALLOW_LINE_SUBSTRINGS
        # should suppress the entire line.
        assert ccl.scan_line(line) == []

    def test_allowlist_does_not_swallow_genuine_leak(self):
        # Allowlist applies per-line; another line with a real codename still flagged.
        line = "TD-030 still pending"  # no allowed substring
        assert ccl.scan_line(line) != []


class TestCodeCommentSkip:
    @pytest.mark.parametrize(
        "line,suffix,should_skip",
        [
            ("# TD-030 comment", ".py", True),
            ("    # nested # TD-030", ".py", True),
            ("// TD-030 in JS", ".js", True),
            ("// TD-030 in Go", ".go", True),
            ("# shell comment HA-11", ".sh", True),
            # Not a comment — just contains # in middle
            ("var = 'TD-030'  # noqa", ".py", False),
            # Markdown — HTML comments visible to GitHub raw viewer, NOT skipped
            ("<!-- TD-030 hidden in markdown -->", ".md", False),
            # Plain text — not a recognized comment syntax
            ("TD-030 in plain text", ".md", False),
        ],
    )
    def test_code_comment_detection(self, line, suffix, should_skip):
        assert ccl._is_code_comment(line, suffix) is should_skip


# ---------------------------------------------------------------------------
# Diff parsing (in _lint_helpers)
# ---------------------------------------------------------------------------
class TestDiffParsing:
    def test_simple_added_line(self):
        diff = (
            "diff --git a/foo.md b/foo.md\n"
            "--- a/foo.md\n"
            "+++ b/foo.md\n"
            "@@ -10,0 +11 @@\n"
            "+New line content\n"
        )
        added = _parse_unified_zero_diff(diff)
        assert added == [(11, "New line content")]

    def test_multiple_added_lines_in_one_hunk(self):
        diff = (
            "@@ -5,0 +6,3 @@\n"
            "+line a\n"
            "+line b\n"
            "+line c\n"
        )
        added = _parse_unified_zero_diff(diff)
        assert added == [(6, "line a"), (7, "line b"), (8, "line c")]

    def test_deletion_only_no_added(self):
        diff = (
            "@@ -10,2 +9,0 @@\n"
            "-removed a\n"
            "-removed b\n"
        )
        assert _parse_unified_zero_diff(diff) == []

    def test_mixed_hunks_distinct_line_numbers(self):
        diff = (
            "@@ -1,1 +1,1 @@\n"
            "-old line 1\n"
            "+new line 1\n"
            "@@ -10,0 +11 @@\n"
            "+later addition\n"
        )
        added = _parse_unified_zero_diff(diff)
        assert added == [(1, "new line 1"), (11, "later addition")]

    def test_empty_diff_returns_empty(self):
        assert _parse_unified_zero_diff("") == []


# ---------------------------------------------------------------------------
# Bypass tag parsing (in _lint_helpers)
# ---------------------------------------------------------------------------
class TestBypassTag:
    def test_basic_bypass_matched(self):
        body = (
            "## Summary\n\n"
            "bypass-lint: codename-leak\n"
            "reason: This citation requires the historical codename for accuracy in the audit-trail discussion.\n"
        )
        result = parse_bypass_tag(body, "codename-leak")
        assert result is not None
        assert "historical codename" in result

    def test_bypass_with_optional_issue(self):
        body = (
            "bypass-lint: codename-leak\n"
            "reason: Legitimate use of internal id for design history reference.\n"
            "issue: #999\n"
        )
        result = parse_bypass_tag(body, "codename-leak")
        assert result == "Legitimate use of internal id for design history reference."

    def test_wrong_lint_name_not_matched(self):
        body = (
            "bypass-lint: some-other-lint\n"
            "reason: Not for codename-leak.\n"
        )
        assert parse_bypass_tag(body, "codename-leak") is None

    def test_case_insensitive_match(self):
        body = (
            "BYPASS-LINT: Codename-Leak\n"
            "Reason: Mixed case should match.\n"
        )
        assert parse_bypass_tag(body, "codename-leak") is not None

    def test_no_bypass_in_body(self):
        body = "This PR is straightforward, no bypass needed."
        assert parse_bypass_tag(body, "codename-leak") is None

    def test_empty_body(self):
        assert parse_bypass_tag("", "codename-leak") is None
        assert parse_bypass_tag(None, "codename-leak") is None

    def test_multiple_bypasses_correct_one_returned(self):
        body = (
            "bypass-lint: other-lint\n"
            "reason: For some other lint.\n\n"
            "bypass-lint: codename-leak\n"
            "reason: For codename leak.\n"
        )
        result = parse_bypass_tag(body, "codename-leak")
        assert result == "For codename leak."
