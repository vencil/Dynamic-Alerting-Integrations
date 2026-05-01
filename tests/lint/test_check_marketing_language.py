"""Tests for check_marketing_language.py — codifies dev-rules.md §6.

Pinned contracts
----------------
1. **Detection**: each banned keyword (zh + en curated list) emits a
   finding when present in source. Multiple matches per line each fire.

2. **Suppression**:
   - inline-code (single backticks) on the matching column → skipped
   - fenced code block (between ```...```) → skipped
   - line containing `<!-- marketing-language: ignore -->` → skipped
   - 3-line lookback for ignore marker (matches PR #166 / #169
     conventions)

3. **Robustness**:
   - empty file → no findings
   - file with only fenced blocks → no findings
   - non-UTF-8 bytes → errors='replace' fallback (no crash)

4. **Severity matrix** (`_compute_exit_code`):
   - !ci, * → exit 0
   - ci, 0 findings → exit 0
   - ci, >0 findings → exit 1

5. **Live dogfood** (`TestLiveDocs`): scans the actual repo's docs and
   confirms zero hits before merge — the lint is fatal under `--ci`,
   so this gate prevents shipping a broken state.
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

import check_marketing_language as cml  # noqa: E402


def _scan(source: str, fake_path: str = "fake.md"):
    return cml.scan_source(Path(fake_path), source)


# ---------------------------------------------------------------------------
# Detection — each banned keyword fires
# ---------------------------------------------------------------------------
class TestKeywordDetection:
    @pytest.mark.parametrize("kw", ["業界領先", "革命性", "獨步全球", "唯一選擇", "顛覆性"])
    def test_zh_keyword_flagged(self, kw):
        src = f"我們的產品是{kw}的解決方案。\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].keyword == kw
        assert findings[0].line == 1

    @pytest.mark.parametrize(
        "kw",
        [
            "industry-leading",
            "revolutionary",
            "game-changing",
            "world-class",
            "best-in-class",
            "cutting-edge",
            "world's first",
            "next-generation",
        ],
    )
    def test_en_keyword_flagged(self, kw):
        src = f"This is a {kw} platform.\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].keyword == kw

    def test_en_match_is_case_insensitive(self):
        # Keyword list is lowercased; uppercase user text still matches.
        src = "Our REVOLUTIONARY new approach.\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].keyword == "revolutionary"

    def test_multiple_keywords_one_line(self):
        src = "革命性的 industry-leading 業界領先 platform.\n"
        findings = _scan(src)
        # Three distinct kw matches — order matches scan order.
        assert len(findings) == 3
        assert {f.keyword for f in findings} == {"革命性", "industry-leading", "業界領先"}

    def test_multiple_keywords_multiple_lines(self):
        src = (
            "Line one is 業界領先.\n"
            "Line two has revolutionary stuff.\n"
            "Line three is fine.\n"
        )
        findings = _scan(src)
        assert len(findings) == 2
        lines = sorted(f.line for f in findings)
        assert lines == [1, 2]

    def test_no_keywords_no_findings(self):
        src = "This is normal technical prose explaining a feature.\n"
        assert _scan(src) == []


# ---------------------------------------------------------------------------
# Suppression — inline code / fenced blocks / ignore marker
# ---------------------------------------------------------------------------
class TestSuppression:
    def test_inline_code_span_suppresses(self):
        src = "Don't write `業界領先` in commit messages.\n"
        # The keyword appears INSIDE backticks → suppress.
        assert _scan(src) == []

    def test_inline_code_suppress_only_when_inside(self):
        # Mixed: 業界領先 outside backticks (fires), `革命性` inside (skip).
        src = "業界領先 is bad, but `革命性` is illustrative.\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].keyword == "業界領先"

    def test_fenced_code_block_suppresses(self):
        src = (
            "Some prose.\n"
            "```\n"
            "業界領先 is just example text in code block\n"
            "```\n"
            "More prose.\n"
        )
        assert _scan(src) == []

    def test_fenced_code_block_does_not_suppress_outside(self):
        src = (
            "```\n"
            "業界領先 inside fence\n"
            "```\n"
            "革命性 outside fence\n"
        )
        findings = _scan(src)
        # Inside fence → suppressed, outside → fires.
        assert len(findings) == 1
        assert findings[0].keyword == "革命性"
        assert findings[0].line == 4

    def test_ignore_comment_same_line(self):
        src = "業界領先 example. <!-- marketing-language: ignore -->\n"
        assert _scan(src) == []

    def test_ignore_comment_3_lines_above(self):
        src = (
            "<!-- marketing-language: ignore -->\n"
            "rationale line 1\n"
            "rationale line 2\n"
            "業界領先 example.\n"  # Line 4 — within 3-line lookback (4-3=1).
        )
        assert _scan(src) == []

    def test_ignore_comment_outside_3_line_lookback(self):
        src = (
            "<!-- marketing-language: ignore -->\n"
            "rationale line 1\n"
            "rationale line 2\n"
            "rationale line 3\n"
            "rationale line 4\n"
            "業界領先 example.\n"  # Line 6 — too far (6-1=5 > 3).
        )
        findings = _scan(src)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
class TestRobustness:
    def test_empty_file(self):
        assert _scan("") == []

    def test_only_fenced_blocks(self):
        src = (
            "```\n"
            "業界領先\n"
            "革命性\n"
            "```\n"
        )
        assert _scan(src) == []

    def test_unclosed_fence_treated_as_open(self):
        # Unclosed fence: line after ``` is treated as fenced (suppressed).
        # This is intentional graceful-degradation: an author opened a
        # block, content inside is illustrative.
        src = (
            "```\n"
            "業界領先 inside unclosed fence\n"
        )
        assert _scan(src) == []

    def test_keyword_at_line_start(self):
        src = "業界領先 starts the line.\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].col == 1


# ---------------------------------------------------------------------------
# Severity matrix
# ---------------------------------------------------------------------------
class TestComputeExitCode:
    @pytest.mark.parametrize("n", [0, 1, 5])
    def test_no_ci_always_exit_0(self, n):
        assert cml._compute_exit_code(ci=False, n_findings=n) == 0

    def test_ci_zero_findings_exit_0(self):
        assert cml._compute_exit_code(ci=True, n_findings=0) == 0

    @pytest.mark.parametrize("n", [1, 5, 100])
    def test_ci_with_findings_exit_1(self, n):
        assert cml._compute_exit_code(ci=True, n_findings=n) == 1


# ---------------------------------------------------------------------------
# main() integration — argparse + exit code wiring
# ---------------------------------------------------------------------------
class TestMain:
    @pytest.mark.timeout(10)
    def test_main_clean_file_exits_0(self, tmp_path, capsys, monkeypatch):
        clean = tmp_path / "clean.md"
        clean.write_text("# Title\n\nNormal technical prose.\n", encoding="utf-8")
        rc = cml.main(["--ci", str(clean)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no marketing-language hits" in out

    @pytest.mark.timeout(10)
    def test_main_dirty_file_under_ci_exits_1(
        self, tmp_path, capsys, monkeypatch
    ):
        dirty = tmp_path / "dirty.md"
        dirty.write_text("我們的產品是業界領先的。\n", encoding="utf-8")
        rc = cml.main(["--ci", str(dirty)])
        err = capsys.readouterr().err
        assert rc == 1
        assert "業界領先" in err

    @pytest.mark.timeout(10)
    def test_main_dirty_file_no_ci_exits_0(self, tmp_path, capsys):
        dirty = tmp_path / "dirty.md"
        dirty.write_text("我們的產品是業界領先的。\n", encoding="utf-8")
        rc = cml.main([str(dirty)])
        # Audit mode never fails.
        assert rc == 0


# ---------------------------------------------------------------------------
# Live dogfood — the actual repo must pass under --ci
# ---------------------------------------------------------------------------
class TestLiveDocs:
    """Ultimate dogfood: run the lint against the actual repo. If this
    PR's edits to dev-rules.md don't fully eliminate hits, this test
    fails — preventing the PR from landing in a broken state."""

    @pytest.mark.timeout(30)
    def test_live_repo_has_no_marketing_language(self):
        # Scan a representative subset to keep test fast (full scan
        # is O(180 files) which is ok but bounded).
        candidates = list(cml._iter_default_files())
        if not candidates:
            pytest.skip("No default files matched; repo layout differs")

        all_findings = []
        for path in candidates:
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            all_findings.extend(cml.scan_source(path, source))

        if all_findings:
            preview = "\n".join(f"  - {f.render()}" for f in all_findings[:10])
            extra = (
                f"\n  ... and {len(all_findings) - 10} more"
                if len(all_findings) > 10
                else ""
            )
            assert False, (
                f"Live repo has {len(all_findings)} marketing-language hit(s):\n"
                f"{preview}{extra}\n\n"
                "Add `<!-- marketing-language: ignore -->` to suppress legitimate\n"
                "anti-pattern quotes, or rewrite to objective language."
            )
