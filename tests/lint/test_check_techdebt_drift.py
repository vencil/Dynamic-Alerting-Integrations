"""Smoke tests for check_techdebt_drift.py — registry vs git-log drift detection.

Covers:
  - `normalize_status` strips markdown emphasis + lowercases
  - `parse_registry` extracts {id: status} from §TECH-DEBT-XXX headings
  - `TRAILER_RE` `\\b` hardening: TECH-DEBT-007 does not match TECH-DEBT-0071
  - `detect_drift` classifies Class A (open-but-resolved) / Class B
  - `parse_git_log` uses encoding="utf-8" safely on CJK commit bodies
"""
from __future__ import annotations

import os
import subprocess
import sys
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_techdebt_drift as ctd  # noqa: E402


# ---------------------------------------------------------------------------
# normalize_status
# ---------------------------------------------------------------------------
class TestNormalizeStatus:
    def test_strips_bold_markers(self):
        assert ctd.normalize_status("**resolved**") == "resolved"

    def test_strips_backticks(self):
        assert ctd.normalize_status("`open`") == "open"

    def test_lowercases(self):
        assert ctd.normalize_status("RESOLVED") == "resolved"

    def test_mixed(self):
        assert ctd.normalize_status("  **`IN-PROGRESS`**  ") == "in-progress"


# ---------------------------------------------------------------------------
# parse_registry
# ---------------------------------------------------------------------------
class TestParseRegistry:
    def test_missing_file_returns_empty(self, tmp_path):
        assert ctd.parse_registry(tmp_path / "nope.md") == {}

    def test_basic_block(self, tmp_path):
        f = tmp_path / "reg.md"
        f.write_text(
            "# Known regressions\n\n"
            "### TECH-DEBT-005：some title\n\n"
            "| field | value |\n"
            "|---|---|\n"
            "| `status` | `open` |\n"
            "| owner | maintainer |\n\n"
            "### REG-003: another title\n\n"
            "| `status` | **resolved** |\n",
            encoding="utf-8",
        )
        result = ctd.parse_registry(f)
        assert result == {"TECH-DEBT-005": "open", "REG-003": "resolved"}

    def test_only_first_status_per_block_captured(self, tmp_path):
        """detail blocks sometimes repeat the status in §4 summary; only first wins."""
        f = tmp_path / "reg.md"
        f.write_text(
            "### TECH-DEBT-010：...\n\n"
            "| `status` | resolved |\n"
            "later restated:\n"
            "| status | open |\n",
            encoding="utf-8",
        )
        assert ctd.parse_registry(f) == {"TECH-DEBT-010": "resolved"}


# ---------------------------------------------------------------------------
# TRAILER_RE word boundary hardening (L4 fix)
# ---------------------------------------------------------------------------
class TestTrailerRegex:
    def test_matches_plain(self):
        m = ctd.TRAILER_RE.search("Resolves TECH-DEBT-007 per PR review")
        assert m and m.group(1) == "TECH-DEBT-007"

    def test_matches_case_insensitive(self):
        m = ctd.TRAILER_RE.search("fixes reg-003")
        assert m and m.group(1).upper() == "REG-003"

    def test_multi_digit_id_captured_in_full(self):
        """Sanity check that the greedy \\d+ captures the full numeric suffix,
        not a prefix (this held even without the \\b; kept as regression guard)."""
        m = ctd.TRAILER_RE.search("Resolves TECH-DEBT-0071 hypothetical")
        assert m and m.group(1) == "TECH-DEBT-0071"

    def test_does_not_match_trailing_junk(self):
        """The `\\b` hardening's actual job: Resolves TECH-DEBT-007x should
        not match at all, because `7` -> `x` is not a word boundary.
        Without `\\b` the regex would wrongly capture TECH-DEBT-007."""
        m = ctd.TRAILER_RE.search("Resolves TECH-DEBT-007x garbage")
        assert m is None


# ---------------------------------------------------------------------------
# detect_drift
# ---------------------------------------------------------------------------
class TestDetectDrift:
    def test_class_a_open_with_commit(self):
        registry = {"TECH-DEBT-005": "open"}
        refs = {"TECH-DEBT-005": ["abc1234"]}
        class_a, class_b = ctd.detect_drift(registry, refs)
        assert class_a == [("TECH-DEBT-005", "open", ["abc1234"])]
        assert class_b == []

    def test_class_b_resolved_without_commit(self):
        registry = {"REG-003": "resolved"}
        refs: dict[str, list[str]] = {}
        class_a, class_b = ctd.detect_drift(registry, refs)
        assert class_a == []
        assert class_b == ["REG-003"]

    def test_neither_when_open_and_no_commit(self):
        registry = {"TECH-DEBT-020": "open"}
        class_a, class_b = ctd.detect_drift(registry, {})
        assert class_a == [] and class_b == []

    def test_neither_when_resolved_with_commit(self):
        registry = {"TECH-DEBT-007": "resolved"}
        refs = {"TECH-DEBT-007": ["def5678"]}
        class_a, class_b = ctd.detect_drift(registry, refs)
        assert class_a == [] and class_b == []


# ---------------------------------------------------------------------------
# parse_git_log — subprocess UTF-8 hardening (H3 fix)
# ---------------------------------------------------------------------------
class TestParseGitLog:
    def test_subprocess_call_uses_utf8_encoding(self):
        """Regression guard for H3: subprocess.check_output must pass
        encoding='utf-8' + errors='replace' so CJK commit bodies do not
        crash the parser on Windows cp950/cp932 consoles."""
        cji_body = (
            "fakesha\x00"
            "feat(foo): 中文訊息 with 日本語\n\n"
            "Resolves TECH-DEBT-042\n"
            "\x00\x00"
        )
        with patch("check_techdebt_drift.subprocess.check_output",
                   return_value=cji_body) as mock_co:
            refs = ctd.parse_git_log(since=None)
            assert "TECH-DEBT-042" in refs
            # Verify the caller actually requested UTF-8 decoding.
            _, kwargs = mock_co.call_args
            assert kwargs.get("encoding") == "utf-8"
            assert kwargs.get("errors") == "replace"

    def test_git_not_found_returns_empty(self):
        """FileNotFoundError from git missing should degrade gracefully."""
        with patch("check_techdebt_drift.subprocess.check_output",
                   side_effect=FileNotFoundError):
            assert ctd.parse_git_log(since=None) == {}

    def test_git_error_returns_empty(self):
        with patch("check_techdebt_drift.subprocess.check_output",
                   side_effect=subprocess.CalledProcessError(1, ["git"])):
            assert ctd.parse_git_log(since=None) == {}
