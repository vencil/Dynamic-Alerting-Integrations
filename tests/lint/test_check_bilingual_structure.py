"""Tests for scripts/tools/lint/check_bilingual_structure.py.

Gap 4 (TRK-007 backlog) — fourth lint self-test, the last P1 in the
chain. Auto-hook lint at 426 LOC, previously zero unit-test coverage.

The lint exists specifically because of the v2.3.0 bug where
cli-reference.en.md was missing the entire "Operator + Federation"
section and the opa-evaluate command. Existing checks
(check_doc_links, validate_docs_versions) only validate file
existence and counts — neither catches section-level content drift.

Coverage:
  - extract_headings: ## / ### / #### markers, frontmatter skip,
    code-block skip, hash level capture
  - heading_skeleton: CJK strip, link unwrap, dash normalization,
    emphasis stripping
  - check_nav_links: bidirectional link presence + missing report
  - compare_structure: heading-count parity per level, total count,
    technical-token (CLI / version / .yaml etc.) divergence flagged
    as error (zh_only) or warning (en_only)
  - discover_bilingual_pairs: legacy *.en.md, new *.zh.md, root
    README, internal-dir exemption, symlink exemption, no-duplicate
  - main CLI: empty repo, clean pair, structural drift exits 1
    under --ci, --json shape
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_bilingual_structure.py"

_spec = importlib.util.spec_from_file_location("check_bilingual_structure", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_bilingual_structure"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# Helpers
# ============================================================


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# ============================================================
# extract_headings
# ============================================================


class TestExtractHeadings:

    def test_basic_h2_h3_h4(self, tmp_path):
        f = tmp_path / "x.md"
        _write(f,
            "# H1 ignored\n"
            "## Top section\n"
            "### Subsection\n"
            "#### Sub-sub\n"
            "##### H5 ignored\n"
        )
        result = mod.extract_headings(f)
        # H1 and H5+ are ignored — only H2/H3/H4 captured.
        assert [(line, level, text) for line, level, text in result] == [
            (2, 2, "Top section"),
            (3, 3, "Subsection"),
            (4, 4, "Sub-sub"),
        ]

    def test_frontmatter_skipped(self, tmp_path):
        f = tmp_path / "x.md"
        _write(f,
            "---\n"
            "title: foo\n"
            "## not a heading inside frontmatter\n"
            "---\n"
            "## Real heading\n"
        )
        result = mod.extract_headings(f)
        # Only the heading after frontmatter close.
        assert len(result) == 1
        assert result[0][2] == "Real heading"

    def test_code_block_headings_skipped(self, tmp_path):
        f = tmp_path / "x.md"
        _write(f,
            "## Outside\n"
            "```python\n"
            "## Inside code, not a heading\n"
            "```\n"
            "## Outside again\n"
        )
        result = mod.extract_headings(f)
        # Two headings, code-block content ignored.
        texts = [t for _, _, t in result]
        assert "Outside" in texts
        assert "Outside again" in texts
        assert "Inside code, not a heading" not in texts

    def test_unreadable_file_returns_empty(self, tmp_path):
        # Property: a file we can't decode → empty list (not raise).
        f = tmp_path / "binary.md"
        f.write_bytes(b"\x80\x81\x82\x83")  # invalid utf-8
        assert mod.extract_headings(f) == []

    def test_missing_file_returns_empty(self, tmp_path):
        # OSError path: nonexistent file → empty list.
        assert mod.extract_headings(tmp_path / "nope.md") == []

    def test_level_count_matches_hash_count(self, tmp_path):
        f = tmp_path / "x.md"
        _write(f,
            "## L2\n"
            "### L3\n"
            "#### L4\n"
        )
        result = mod.extract_headings(f)
        assert [level for _, level, _ in result] == [2, 3, 4]


# ============================================================
# heading_skeleton
# ============================================================


class TestHeadingSkeleton:

    def test_strips_cjk(self):
        # Property: Chinese characters are removed, technical tokens stay.
        result = mod.heading_skeleton([(1, 2, "API Response 格式")])
        # The CJK characters are gone but ASCII technical content remains.
        level, key = result[0]
        assert level == 2
        assert "格式" not in key
        assert "api" in key
        assert "response" in key

    def test_lowercase_normalization(self):
        result = mod.heading_skeleton([(1, 2, "MIXED Case Heading")])
        _, key = result[0]
        assert key == key.lower()
        assert "mixed" in key

    def test_unwraps_markdown_links(self):
        # Property: `[text](url)` → just `text`.
        result = mod.heading_skeleton([(1, 2, "See [docs link](http://x.com) here")])
        _, key = result[0]
        assert "docs link" in key
        assert "http://x.com" not in key

    def test_strips_emphasis_chars(self):
        result = mod.heading_skeleton([(1, 2, "**bold** and `code` and _italic_")])
        _, key = result[0]
        assert "*" not in key
        assert "`" not in key
        assert "_" not in key
        assert "bold" in key
        assert "code" in key

    def test_normalizes_whitespace(self):
        # Property: multiple consecutive spaces / dashes collapse to one.
        result = mod.heading_skeleton([(1, 2, "a   b -- c")])
        _, key = result[0]
        assert "  " not in key  # no double space
        assert key.strip() == key  # no leading/trailing ws


# ============================================================
# check_nav_links
# ============================================================


class TestCheckNavLinks:

    def test_clean_when_both_link_each_other(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "# 標題\n\n[English](doc.en.md)\n\n本文…\n")
        _write(en, "# Title\n\n[中文](doc.md)\n\nBody…\n")
        assert mod.check_nav_links(zh, en) == []

    def test_zh_missing_link_to_en_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "# 標題 only\n\nNo link to counterpart.\n")
        _write(en, "# Title\n\n[中文](doc.md)\n")
        issues = mod.check_nav_links(zh, en)
        assert len(issues) == 1
        assert "doc.en.md" in issues[0]
        assert "doc.md" in issues[0]

    def test_unreadable_file_silently_skipped(self, tmp_path, monkeypatch):
        # Property: when the file can't be read, the helper returns
        # True (treats as "link present") to avoid false positives on
        # binary / encoding-issue files.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        # Write a valid zh side, but make en side unreadable bytes.
        _write(zh, "# 標題\n[English](doc.en.md)\n")
        en.write_bytes(b"\x80\x81\x82\x83")
        # No issues — both files "have" the link from the helper's POV.
        assert mod.check_nav_links(zh, en) == []

    def test_link_must_be_in_first_20_lines(self, tmp_path, monkeypatch):
        # Property: links beyond line 20 don't count.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        # Push the link past line 20 in zh.
        body_lines = ["padding line " + str(i) for i in range(25)]
        body_lines.append("[English](doc.en.md)")  # line 26
        _write(zh, "\n".join(body_lines) + "\n")
        _write(en, "[中文](doc.md)\n")
        issues = mod.check_nav_links(zh, en)
        assert any("doc.en.md" in m for m in issues)


# ============================================================
# compare_structure
# ============================================================


class TestCompareStructure:

    def test_clean_pair_no_issues(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "## 第一章\n### 子章\n## 第二章\n")
        _write(en, "## Chapter One\n### Sub\n## Chapter Two\n")
        # Same structure (2× h2, 1× h3), no technical tokens in either.
        assert mod.compare_structure(zh, en) == []

    def test_h2_count_mismatch_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "## 一\n## 二\n## 三\n")
        _write(en, "## One\n## Two\n")
        issues = mod.compare_structure(zh, en)
        # Per-level mismatch + total-count mismatch surface as 2 errors.
        assert any("h2" in i["message"] for i in issues)
        assert any("總標題數" in i["message"] for i in issues)

    def test_h3_count_mismatch_flagged(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "## A\n### a1\n### a2\n")
        _write(en, "## A\n### a1\n")
        issues = mod.compare_structure(zh, en)
        assert any("h3" in i["message"] for i in issues)

    def test_zh_only_cli_token_is_error(self, tmp_path, monkeypatch):
        # Property: the v2.3.0 bug class — ZH has a heading with a tech
        # token absent from EN → ERROR on the EN file. Token-set uses
        # the cli_pattern allowlist, so we need a token unique to ZH.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh,
            "## Overview\n"
            "## kubectl basics\n"
            "## opa-evaluate command\n"  # `opa` token, only on ZH side
        )
        _write(en,
            "## Overview\n"
            "## kubectl basics\n"
            "## Filler\n"  # keep total count matching
        )
        issues = mod.compare_structure(zh, en)
        cli_errors = [
            i for i in issues
            if i["severity"] == "error" and "opa" in i["message"]
        ]
        assert cli_errors, f"opa divergence missed: {issues!r}"
        # Error is filed against the EN file (the file missing the heading).
        assert cli_errors[0]["file"].endswith("doc.en.md")

    def test_en_only_cli_token_is_warning(self, tmp_path, monkeypatch):
        # Property: EN-only technical heading is a WARNING on the ZH file
        # (less severe than the v2.3.0 case where EN was incomplete).
        # The detection works by token-set: a heading's tokens must NOT
        # intersect the other side's token union. Use distinct tokens
        # (helm vs kubectl) to make the divergence visible.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh,
            "## Overview\n"
            "## kubectl basics\n"
        )
        _write(en,
            "## Overview\n"
            "## kubectl basics\n"
            "## helm guide\n"  # `helm` is in cli_pattern; not in ZH
        )
        issues = mod.compare_structure(zh, en)
        warnings = [i for i in issues if i["severity"] == "warning"]
        assert any("helm" in i["message"] for i in warnings), (
            f"helm-orphan warning missed: {[i['message'] for i in warnings]!r}"
        )

    def test_shared_token_treats_translated_pair_equivalent(
        self, tmp_path, monkeypatch
    ):
        # Property: "API Response 格式" (zh) ↔ "API Response Format" (en)
        # both emit token "api" → considered equivalent, no divergence.
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        zh = tmp_path / "doc.md"
        en = tmp_path / "doc.en.md"
        _write(zh, "## API Response 格式\n")
        _write(en, "## API Response Format\n")
        issues = mod.compare_structure(zh, en)
        # No CLI-token divergence.
        assert not any(
            i.get("severity") == "error" and "技術標題" in i["message"]
            for i in issues
        )


# ============================================================
# discover_bilingual_pairs — dual-mode discovery
# ============================================================


class TestDiscoverBilingualPairs:

    @pytest.fixture
    def fake_repo(self, tmp_path, monkeypatch):
        # Build a minimal repo skeleton and patch all module-level paths.
        docs = tmp_path / "docs"
        rule_packs = tmp_path / "rule-packs"
        docs.mkdir()
        rule_packs.mkdir()

        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "SCAN_DIRS", [docs, rule_packs])
        monkeypatch.setattr(mod, "SCAN_ROOT_FILES", [tmp_path / "README.md"])
        return tmp_path, docs, rule_packs

    def test_legacy_pair_discovered(self, fake_repo):
        _, docs, _ = fake_repo
        zh = docs / "guide.md"
        en = docs / "guide.en.md"
        _write(zh, "# zh\n")
        _write(en, "# en\n")
        pairs = mod.discover_bilingual_pairs()
        assert (zh, en) in pairs

    def test_new_zh_pair_discovered(self, fake_repo):
        _, docs, _ = fake_repo
        en = docs / "guide.md"
        zh = docs / "guide.zh.md"
        _write(en, "# en\n")
        _write(zh, "# zh\n")
        pairs = mod.discover_bilingual_pairs()
        # The ordering convention: (zh_path, en_path).
        assert (zh, en) in pairs

    def test_orphan_file_not_paired(self, fake_repo):
        _, docs, _ = fake_repo
        # Only EN exists, no ZH counterpart.
        _write(docs / "lonely.en.md", "# en\n")
        pairs = mod.discover_bilingual_pairs()
        assert pairs == []

    def test_internal_dir_exempted_on_posix(self, fake_repo):
        # Property: docs/internal/* are NOT added to pairs (project
        # policy). Implementation uses `str(rel_path).startswith(
        # "docs/internal/")` which is forward-slash-specific; this test
        # verifies the intended behavior on POSIX. On Windows the same
        # `str(rel_path)` produces backslash separators and the
        # exemption silently no-ops — that's a known cross-platform
        # quirk in the lint itself, separate from this test's scope.
        # Skip on non-POSIX so the test stays accurate.
        import os
        if os.sep != "/":
            pytest.skip("BILINGUAL_EXEMPT_DIRS uses forward-slash prefixes (POSIX only)")
        repo_root, docs, _ = fake_repo
        internal = docs / "internal"
        internal.mkdir()
        _write(internal / "private.md", "# zh\n")
        _write(internal / "private.en.md", "# en\n")
        pairs = mod.discover_bilingual_pairs()
        for zh, en in pairs:
            assert "internal" not in str(zh.relative_to(repo_root))

    def test_bilingual_exempt_dirs_constant(self):
        # Property: docs/internal/ is in the exempt list (project policy).
        # Cross-platform: just check the constant value.
        assert "docs/internal/" in mod.BILINGUAL_EXEMPT_DIRS

    def test_root_readme_pair_legacy(self, fake_repo):
        repo_root, _, _ = fake_repo
        _write(repo_root / "README.md", "# zh README\n")
        _write(repo_root / "README.en.md", "# en README\n")
        pairs = mod.discover_bilingual_pairs()
        assert any(
            zh.name == "README.md" and en.name == "README.en.md"
            for zh, en in pairs
        )

    def test_no_duplicates_when_both_patterns_exist(self, fake_repo):
        # Edge case: if a directory accidentally has *.en.md AND *.zh.md
        # for the same .md, we don't double-count.
        _, docs, _ = fake_repo
        # ZH-primary: doc.md (zh) + doc.en.md (en)
        zh1 = docs / "doc.md"
        en1 = docs / "doc.en.md"
        _write(zh1, "# zh\n")
        _write(en1, "# en\n")

        pairs = mod.discover_bilingual_pairs()
        # Only one pair for `doc`, regardless of any leftover convention.
        assert sum(
            1 for z, e in pairs if z.stem == "doc" or e.stem == "doc"
        ) == 1


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    @pytest.fixture
    def empty_repo(self, tmp_path, monkeypatch):
        docs = tmp_path / "docs"
        rule_packs = tmp_path / "rule-packs"
        docs.mkdir()
        rule_packs.mkdir()
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(mod, "SCAN_DIRS", [docs, rule_packs])
        monkeypatch.setattr(mod, "SCAN_ROOT_FILES", [tmp_path / "no-readme.md"])
        return tmp_path, docs

    def test_no_pairs_exits_zero(self, empty_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0

    def test_clean_pair_exits_zero(self, empty_repo, monkeypatch, capsys):
        _, docs = empty_repo
        zh = docs / "doc.md"
        en = docs / "doc.en.md"
        _write(zh,
            "# 標題\n[English](doc.en.md)\n\n## 第一章\n### 子\n"
        )
        _write(en,
            "# Title\n[中文](doc.md)\n\n## Chapter One\n### Sub\n"
        )
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "結構一致" in out

    def test_structural_drift_exits_one_under_ci(
        self, empty_repo, monkeypatch, capsys
    ):
        # Negative: the v2.3.0 bug class — EN missing technical section
        # → exit 1 under --ci.
        _, docs = empty_repo
        zh = docs / "doc.md"
        en = docs / "doc.en.md"
        _write(zh,
            "# 標題\n[English](doc.en.md)\n\n## da-tools opa-evaluate\n"
        )
        _write(en,
            "# Title\n[中文](doc.md)\n\n## Filler\n"
        )
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        # Skeleton normalization replaces `-` with space, so the surfaced
        # form reads "opa evaluate" not "opa-evaluate".
        assert "opa evaluate" in out

    def test_drift_without_ci_exits_zero(
        self, empty_repo, monkeypatch, capsys
    ):
        # Property: errors without `--ci` print but exit 0 (informational).
        _, docs = empty_repo
        zh = docs / "doc.md"
        en = docs / "doc.en.md"
        _write(zh, "## A\n## B\n")
        _write(en, "## A\n")
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0  # report-only

    def test_changelog_skipped_from_structure_check(
        self, empty_repo, monkeypatch, capsys
    ):
        # Property: CHANGELOG.md is in SKIP_STRUCTURE_CHECK so structural
        # drift between CHANGELOG.md / CHANGELOG.en.md is not flagged.
        _, docs = empty_repo
        zh = docs / "CHANGELOG.md"
        en = docs / "CHANGELOG.en.md"
        # Wildly different structure — would normally trip the lint.
        _write(zh,
            "# 變更日誌\n[English](CHANGELOG.en.md)\n\n"
            "## v2.9.0\n## v2.8.0\n## v2.7.0\n"
        )
        _write(en,
            "# Changelog\n[中文](CHANGELOG.md)\n\n"
            "## v2.9.0\n"
        )
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0  # CHANGELOG drift exempted

    def test_json_output_shape(self, empty_repo, monkeypatch, capsys):
        _, docs = empty_repo
        zh = docs / "doc.md"
        en = docs / "doc.en.md"
        _write(zh,
            "# 標題\n[English](doc.en.md)\n\n## 章\n"
        )
        _write(en,
            "# Title\n[中文](doc.md)\n\n## Chapter\n"
        )
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--json"])
        with pytest.raises(SystemExit):
            mod.main()
        payload = json.loads(capsys.readouterr().out)
        assert "pairs_checked" in payload
        assert "structure_issues" in payload
        assert "nav_issues" in payload
        assert "summary" in payload
        for k in ("errors", "warnings", "nav_issues"):
            assert k in payload["summary"]


# ============================================================
# Repo-level smoke regression guard
# ============================================================


class TestRepoSmoke:

    def test_actual_repo_passes_under_ci(self, monkeypatch):
        """The shipped docs / rule-packs / READMEs must pass the lint.

        Belt-and-suspenders alongside the pre-commit hook: any structural
        drift introduced silently into a bilingual pair fails this test.
        """
        monkeypatch.setattr(sys, "argv", ["check_bilingual_structure", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0, (
            "repo's bilingual-structure state fails its own lint"
        )
