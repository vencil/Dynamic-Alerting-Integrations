"""Tests for check_doc_links.py — documentation cross-reference checker."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_doc_links as cdl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_checker(tmp_path, verbose=False):
    """Create a DocLinkChecker with a minimal repo layout."""
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    readme = tmp_path / "README.md"
    readme.write_text("# Readme\n", encoding="utf-8")
    return cdl.DocLinkChecker(str(tmp_path), verbose=verbose)


# ---------------------------------------------------------------------------
# _heading_to_anchor
# ---------------------------------------------------------------------------
class TestHeadingToAnchor:
    def test_basic(self):
        assert cdl.DocLinkChecker._heading_to_anchor("Hello World") == "hello-world"

    def test_strips_markdown(self):
        assert cdl.DocLinkChecker._heading_to_anchor("**Bold** Text") == "bold-text"

    def test_strips_code(self):
        assert cdl.DocLinkChecker._heading_to_anchor("`code` stuff") == "code-stuff"

    def test_strips_links(self):
        result = cdl.DocLinkChecker._heading_to_anchor("[Link](http://example.com)")
        assert result == "link"

    def test_cjk_preserved(self):
        result = cdl.DocLinkChecker._heading_to_anchor("專案概覽")
        assert "專案概覽" in result

    def test_special_chars_removed(self):
        result = cdl.DocLinkChecker._heading_to_anchor("Section (v2.1.0)")
        # Parentheses removed, dots and numbers kept
        assert "section" in result
        assert "(" not in result

    def test_emoji_shortcode_keeps_the_name(self):
        # github.com deletes the `:` (Po) and keeps the shortcode *name*.
        # The pre-2026 implementation deleted `:[a-z_]+:` wholesale, which
        # invented an anchor nobody could ever link to.
        assert cdl.DocLinkChecker._heading_to_anchor(":rocket: Launch") == (
            "rocket-launch")


# ---------------------------------------------------------------------------
# GFM slug rules — the four behaviours the pre-2026 implementation got wrong,
# plus the ones that keep it honest.  Each expectation is what github.com
# actually emits (see TestGithubSluggerFixtures for the upstream ground truth).
# ---------------------------------------------------------------------------
class TestGfmSlugRules:
    @pytest.mark.parametrize("heading,expected", [
        # (1) Whitespace runs are NOT collapsed — every U+0020 is one `-`.
        ("Hello  World", "hello--world"),
        # ...which is why deleted punctuation between spaces leaves `--`.
        ("Role & Tool", "role--tool"),
        ("conf.d/ Directory Hierarchy + Mixed Mode",
         "confd-directory-hierarchy--mixed-mode"),
        ("Day-0 / Day-1", "day-0--day-1"),
        ("Planning SSOT — Frontmatter Contract",
         "planning-ssot--frontmatter-contract"),
        # (2) `_` (Pc) survives — it is a word character, not markup.
        ("`_defaults.yaml` Inheritance", "_defaultsyaml-inheritance"),
        ("SetMetrics / SetLogger seam", "setmetrics--setlogger-seam"),
        # (3) `:shortcode:` loses only its colons.
        (":ok_hand: Single", "ok_hand-single"),
        # (4) Leading/trailing `-` are NOT stripped.
        ("⛔ 高頻地雷", "-高頻地雷"),
        ("Trailing emoji 🛡️", "trailing-emoji-️"),
        # ASCII `-` survives; other Pd (en/em dash) are deleted outright.
        ("TRK-302 rollout", "trk-302-rollout"),
        ("v2.9.0 — GA", "v290--ga"),
        # CJK (Lo) survives, full-width punctuation does not.
        ("部署 tenant-api + da-portal（Tenant Manager）",
         "部署-tenant-api--da-portaltenant-manager"),
        ("修復層 B：FUSE Cache 重建（Level 1 ~ 5）",
         "修復層-bfuse-cache-重建level-1--5"),
        # Non-U+0020 whitespace is DELETED, not hyphenated.
        ("Foo\tBar", "foobar"),
        ("Foo Bar", "foobar"),
        # Inline markup is reduced to its rendered text first.
        ("**Bold** `code` *it*", "bold-code-it"),
        ("[Link](http://example.com)", "link"),
        ("![alt text](img.png)", "alt-text"),
    ])
    def test_rule(self, heading, expected):
        assert cdl.DocLinkChecker._heading_to_anchor(heading) == expected

    def test_leading_dash_is_load_bearing(self):
        """Regression guard for the `.strip('-')` bug.

        Emoji-prefixed headings are everywhere in this repo; stripping the
        leading dash minted an anchor github.com never emits, so the checker
        happily validated links that 404 on github.com.
        """
        anchor = cdl.DocLinkChecker._heading_to_anchor("🚀 Quick Start")
        assert anchor == "-quick-start"
        assert not anchor.startswith("q")


# ---------------------------------------------------------------------------
# Duplicate-heading numbering (page-scoped `-1` / `-2` suffixes)
# ---------------------------------------------------------------------------
class TestGfmSluggerDuplicates:
    def test_plain_repetition(self):
        slugger = cdl._GfmSlugger()
        assert [slugger.slug("Rollback") for _ in range(3)] == [
            "rollback", "rollback-1", "rollback-2"]

    def test_collision_loop_skips_taken_ids(self):
        """`echo 1` and `echo-1` slug identically — the counter must not
        hand the same id to both (github-slugger's while-loop)."""
        slugger = cdl._GfmSlugger()
        assert slugger.slug("echo") == "echo"
        assert slugger.slug("echo") == "echo-1"
        assert slugger.slug("echo 1") == "echo-1-1"
        assert slugger.slug("echo-1") == "echo-1-2"
        assert slugger.slug("echo") == "echo-2"

    def test_get_headings_exposes_duplicate_variants(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "dup.md"
        md.write_text("# Setup\n\n## Setup\n\n## Setup\n", encoding="utf-8")
        assert checker._get_headings(md) == {"setup", "setup-1", "setup-2"}


# ---------------------------------------------------------------------------
# Block structure: fences and HTML comments
# ---------------------------------------------------------------------------
class TestBlockStructure:
    """Which lines COUNT as headings — the half that corpus coverage can't reach.

    Every case here was a real defect, and not one of them was caught by a test
    or by the repo's own 306 Markdown files: the corpus contains no `~~~` fence,
    no 4-backtick fence, and no heading whose anchors depend on comment handling.
    A rewrite of this machinery is therefore invisible to CI unless these live
    as fixtures, which is exactly how the last two rewrites shipped broken.

    Ground truth is CommonMark (verified against markdown-it-py); the failure
    mode on the left of each `==` is what a wrong implementation produced.
    """

    @pytest.mark.parametrize("name,body,expected", [
        # ── fences ──────────────────────────────────────────────────────────
        ("tilde fence hides its comments",
         "# Real\n~~~bash\n# Phantom\n~~~\n", {"real"}),
        ("bare fence hides its comments",
         "# Real\n```\n# Phantom\n```\n", {"real"}),
        ("info string does not close a fence",
         "```\n# A\n```bash\n# B\n```\n## After\n", {"after"}),
        ("longer run closes, shorter does not",
         "````\n```\n# Inner\n```\n````\n## After\n", {"after"}),
        ("closing fence may not carry a trailing word",
         "```\n# Inner\n``` foo\n```\n## After\n", {"after"}),
        ("tilde info string MAY contain a backtick",
         "~~~js `x`\n# Inner\n~~~\n## After\n", {"after"}),
        ("unterminated fence swallows to EOF",
         "# Before\n```\n# Inner\n", {"before"}),
        # ── HTML comments ───────────────────────────────────────────────────
        ("a commented-out heading is not rendered",
         "# Top\n<!--\n## Hidden\n-->\n## After\n", {"top", "after"}),
        ("a comment may span a whole fence",
         "<!--\n```\n## X\n```\n-->\n## After\n", {"after"}),
        # ⛔ The four below are why order matters: comments must be resolved
        # INSIDE the fence state, and only a line-leading `<!--` opens a block.
        ("unpaired <!-- inside a fence is literal content",
         "# Real\n```markdown\n<!-- hide\n```\n\n## After\n", {"real", "after"}),
        ("...and does not eat every later heading",
         "```\n<!-- x\n```\n# One\n# Two\n", {"one", "two"}),
        ("<!-- in a heading's code span is inline text",
         "## The `<!--` marker\n\n## After\n", {"the----marker", "after"}),
        ("<!--> is malformed and opens nothing",
         "<!-->\n# After\n", {"after"}),
        ("mid-line unclosed <!-- stays literal",
         "Foo <!--\n# After\n-->\n", {"after"}),
    ])
    def test_heading_visibility(self, tmp_path, name, body, expected):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "case.md"
        md.write_text(body, encoding="utf-8")
        assert checker._get_headings(md) == expected, name


# ---------------------------------------------------------------------------
# Upstream ground truth
# ---------------------------------------------------------------------------
class TestGithubSluggerFixtures:
    """Verbatim cases from `github-slugger`'s test/fixtures.json.

    Copyright (c) 2015 Dan Flettre — ISC License.
    https://github.com/Flet/github-slugger

    Those fixtures are GitHub-*rendered* ground truth, so they pin the exact
    rule: lowercase → delete every char outside
    `Alphabetic | Mark | Decimal_Number | Connector_Punctuation | '-' | ' '`
    → turn each U+0020 into one `-`.

    Cases are fed to `_gfm_slug` (the raw core) rather than
    `_heading_to_anchor`, because the latter additionally `.strip()`s the
    heading — correct for Markdown ATX headings, where surrounding
    whitespace is not part of the rendered text, but not what the raw
    slugger contract says.  The three whitespace-edge cases below are
    therefore listed against the core only.
    """

    # A curated subset — the multi-KB Unicode-category blobs are omitted.
    UPSTREAM = [
        ("bravoCharlieDelta", "bravocharliedelta"),
        ("heading with a - dash", "heading-with-a---dash"),
        ("heading with an _ underscore", "heading-with-an-_-underscore"),
        ("heading with a period.txt", "heading-with-a-periodtxt"),
        ("exchange.bind_headers(exchange, routing [, bindCallback])",
         "exchangebind_headersexchange-routing--bindcallback"),
        (" ", "-"),
        (" a", "-a"),
        ("a ", "a-"),
        ("apostrophe’s should be trimmed",
         "apostrophes-should-be-trimmed"),
        ("I ♥ unicode", "i--unicode"),
        ("dash-dash", "dash-dash"),
        ("en–dash", "endash"),
        ("em–dash", "emdash"),
        ("\U0001f604 unicode emoji", "-unicode-emoji"),
        ("\U0001f604_\U0001f604 unicode emoji", "_-unicode-emoji"),
        ("\U0001f604 - an emoji", "---an-emoji"),
        (":smile: - a gemoji", "smile---a-gemoji"),
        ("Привет non-latin 你好",
         "привет-non-latin-你好"),
        (":ok: No underscore", "ok-no-underscore"),
        (":ok_hand: Single", "ok_hand-single"),
        (":ok_hand::hatched_chick: Two in a row with no spaces",
         "ok_handhatched_chick-two-in-a-row-with-no-spaces"),
        (":ok_hand: :hatched_chick: Two in a row",
         "ok_hand-hatched_chick-two-in-a-row"),
        # Connector_Punctuation — every one kept.
        ("a_ ‿ ⁀ ⁔ ︳ ︴ ﹍ ﹎ ﹏ ＿b",
         "a_-‿-⁀-⁔-︳-︴-﹍-﹎-﹏-＿b"),
        # Dash_Punctuation — only ASCII `-` kept, the other 23 deleted.
        ("a- ֊ ־ ᐀ ᠆ ‐ ‑ ‒ – — "
         "― ⸗ ⸚ ⸺ ⸻ ⹀ 〜 〰 ゠ "
         "︱ ︲ ﹘ ﹣ －b",
         "a------------------------b"),
        # Enclosing_Mark — every one kept (this is why VS16 survives too).
        ("a҈ ҉ ᪾ ⃝ ⃞ ⃟ ⃠ ⃢ ⃣ "
         "⃤ ꙰ ꙱ ꙲b",
         "a҈-҉-᪾-⃝-⃞-⃟-⃠-⃢-⃣-"
         "⃤-꙰-꙱-꙲b"),
        ("a b", "ab"),
    ]

    @pytest.mark.parametrize("raw,expected", UPSTREAM)
    def test_upstream_case(self, raw, expected):
        assert cdl._gfm_slug(raw) == expected

    def test_upstream_repetition_sequence(self):
        """Fixtures 'Repetition (1)'..'(5)' — one slugger, in order."""
        slugger = cdl._GfmSlugger()
        seq = ["echo", "echo", "echo 1", "echo-1", "echo"]
        got = [slugger.dedup(cdl._gfm_slug(s)) for s in seq]
        assert got == ["echo", "echo-1", "echo-1-1", "echo-1-2", "echo-2"]

    def test_known_gap_other_alphabetic_symbols(self):
        """Documented, accepted divergence — see `_GFM_KEEP_CATEGORIES`.

        `Alphabetic` includes Other_Alphabetic, which covers the enclosed
        Latin letters (`ⓔ`, `🅨` — general category So).  stdlib
        `unicodedata` exposes general categories only, so we drop them where
        github.com keeps them.  Closing this would mean either a new
        dependency (the `doc-links-check` hook is deliberately dependency
        free) or a hand-maintained Unicode range table that silently drifts.
        Zero headings in this repo contain such a character.

        This test exists so the gap fails loudly if it ever *changes*,
        rather than sitting undocumented.
        """
        assert cdl._gfm_slug("aⓔb") == "ab"   # github.com: "aⓔb"
        assert cdl._gfm_slug("a\U0001f168b") == "ab"  # github.com: "a🅨b"


# ---------------------------------------------------------------------------
# _is_external_url
# ---------------------------------------------------------------------------
class TestIsExternalUrl:
    def test_http(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("http://example.com") is True

    def test_https(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("https://example.com") is True

    def test_relative(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("../docs/guide.md") is False


# ---------------------------------------------------------------------------
# _resolve_link_path
# ---------------------------------------------------------------------------
class TestResolveLinkPath:
    def test_relative_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        source = tmp_path / "docs" / "guide.md"
        (tmp_path / "docs").mkdir(exist_ok=True)
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "other.md")
        assert valid is True
        assert target == (tmp_path / "docs" / "other.md").resolve()

    def test_anchor_only(self, tmp_path):
        checker = _make_checker(tmp_path)
        source = tmp_path / "docs" / "guide.md"
        (tmp_path / "docs").mkdir(exist_ok=True)
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "#section")
        assert valid is True
        assert target == source

    def test_parent_directory_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        subdir = tmp_path / "docs" / "sub"
        subdir.mkdir(parents=True)
        source = subdir / "nested.md"
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "../guide.md")
        assert valid is True
        assert target == (tmp_path / "docs" / "guide.md").resolve()


# ---------------------------------------------------------------------------
# _load_ignore_file
# ---------------------------------------------------------------------------
class TestLoadIgnoreFile:
    def test_no_ignore_file(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._ignore_patterns == set()

    def test_with_patterns(self, tmp_path):
        (tmp_path / ".doclinkignore").write_text(
            "# comment\nbroken-link.md\ndocs/old.md:../legacy.md\n",
            encoding="utf-8",
        )
        checker = _make_checker(tmp_path)
        assert "broken-link.md" in checker._ignore_patterns
        assert "docs/old.md:../legacy.md" in checker._ignore_patterns

    def test_is_ignored(self, tmp_path):
        (tmp_path / ".doclinkignore").write_text(
            "legacy.md\ndocs/guide.md:../old.md\n", encoding="utf-8"
        )
        checker = _make_checker(tmp_path)
        # Generic match
        assert checker._is_ignored(
            tmp_path / "docs" / "any.md", "legacy.md") is True
        # File-specific match
        assert checker._is_ignored(
            tmp_path / "docs" / "guide.md", "../old.md") is True
        # No match
        assert checker._is_ignored(
            tmp_path / "docs" / "guide.md", "other.md") is False


# ---------------------------------------------------------------------------
# _get_headings
# ---------------------------------------------------------------------------
class TestGetHeadings:
    def test_extracts_headings(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "test.md"
        md.write_text(
            "# Title\n\n## Section A\n\n### Sub Section\n", encoding="utf-8"
        )
        headings = checker._get_headings(md)
        assert "title" in headings
        assert "section-a" in headings
        assert "sub-section" in headings

    def test_skips_code_blocks(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "test.md"
        md.write_text(
            "# Real\n\n```\n# Not a heading\n```\n\n## Also Real\n",
            encoding="utf-8",
        )
        headings = checker._get_headings(md)
        assert "real" in headings
        assert "also-real" in headings
        assert "not-a-heading" not in headings


# ---------------------------------------------------------------------------
# _fuzzy_best
# ---------------------------------------------------------------------------
class TestFuzzyBest:
    def test_exact_match(self):
        result = cdl.DocLinkChecker._fuzzy_best("hello", {"hello", "world"})
        assert result == "hello"

    def test_close_match(self):
        result = cdl.DocLinkChecker._fuzzy_best("helo", {"hello", "world"})
        assert result == "hello"

    def test_below_threshold(self):
        result = cdl.DocLinkChecker._fuzzy_best("xyz", {"hello", "world"})
        assert result == ""

    def test_empty_haystack(self):
        result = cdl.DocLinkChecker._fuzzy_best("hello", set())
        assert result == ""


# ---------------------------------------------------------------------------
# _build_code_block_set
# ---------------------------------------------------------------------------
class TestBuildCodeBlockSet:
    def test_marks_code_lines(self):
        lines = ["text\n", "```\n", "code\n", "```\n", "more text\n"]
        code_set = cdl.DocLinkChecker._build_code_block_set(lines)
        assert 0 not in code_set  # text
        assert 1 in code_set      # opening fence
        assert 2 in code_set      # code
        assert 3 in code_set      # closing fence
        assert 4 not in code_set  # more text

    def test_no_code_blocks(self):
        lines = ["line 1\n", "line 2\n"]
        code_set = cdl.DocLinkChecker._build_code_block_set(lines)
        assert len(code_set) == 0


# ---------------------------------------------------------------------------
# scan_file (integration-level)
# ---------------------------------------------------------------------------
class TestScanFile:
    def test_detects_broken_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [missing](nonexistent.md) for details.\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 1
        assert "nonexistent.md" in str(checker.broken_links[0]["link"])

    def test_valid_link_no_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# Other\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text("See [other](other.md) for details.\n", encoding="utf-8")
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_external_links_skipped(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [Google](https://google.com) for info.\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_code_block_links_skipped(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "```\n[fake](broken.md)\n```\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_broken_anchor_detected(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# Real Heading\n\nContent.\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [section](other.md#nonexistent-heading).\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_anchors) == 1

    def test_valid_anchor_no_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# My Section\n\nContent.\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [section](other.md#my-section).\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_anchors) == 0


# ---------------------------------------------------------------------------
# check_cross_language_counterparts
# ---------------------------------------------------------------------------
class TestCrossLanguageCounterparts:
    def test_paired_files_ok(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "guide.md").write_text("zh", encoding="utf-8")
        (docs / "guide.en.md").write_text("en", encoding="utf-8")
        checker = _make_checker(tmp_path)
        checker.check_cross_language_counterparts()
        assert len(checker.missing_counterparts) == 0

    def test_missing_en_counterpart(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "guide.md").write_text("zh", encoding="utf-8")
        (docs / "other.en.md").write_text("en", encoding="utf-8")  # trigger en_dirs
        checker = _make_checker(tmp_path)
        checker.check_cross_language_counterparts()
        missing = [m for m in checker.missing_counterparts if m["direction"] == "zh→en"]
        assert any("guide" in m["missing"] for m in missing)


# ---------------------------------------------------------------------------
class TestNoInvisibleCharsInLinkAnchors:
    """A link fragment must never carry a non-printing character.

    GitHub's slug keeps Mn/Cf codepoints (a VARIATION SELECTOR-16 that trails an
    emoji is `Mn`) while dropping the emoji itself (`So`), so a heading ending in
    `... 🛡️` yields an anchor whose last character is invisible. Writing that
    anchor into a link is *correct* for GitHub and unreviewable for humans: the
    fragment looks identical to the clean one in every editor and diff.

    The fix is always on the heading side (drop the emoji), never on the link
    side — which is why this guard reads links rather than headings: 53 headings
    in this repo would slug to an invisible-tailed anchor, and they are harmless
    right up until someone links to one.
    """

    _INVISIBLE = ("Mn", "Cf")

    @staticmethod
    def _repo_root() -> Path:
        return Path(__file__).resolve().parents[2]

    def _offenders(self):
        import re
        import unicodedata
        root = self._repo_root()
        skip = {".git", "node_modules", "site", ".pytest_cache"}
        found, scanned = [], 0
        for path in sorted(root.rglob("*.md")):
            if skip & set(path.relative_to(root).parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8").split("\n")
            except OSError:
                continue
            scanned += 1
            for lineno, line in enumerate(lines, 1):
                for m in re.finditer(r"\]\(([^)]*#[^)]*)\)", line):
                    frag = m.group(1)
                    bad = [c for c in frag
                           if unicodedata.category(c) in self._INVISIBLE]
                    if bad:
                        found.append((
                            str(path.relative_to(root)), lineno, frag,
                            [f"U+{ord(c):04X}" for c in bad]))
        return found, scanned

    def test_no_link_fragment_carries_an_invisible_codepoint(self):
        offenders, scanned = self._offenders()
        assert not offenders, (
            "these link fragments contain a non-printing codepoint — the target "
            "heading almost certainly ends in an emoji, whose VARIATION "
            "SELECTOR survives GitHub's slug while the emoji itself does not. "
            "FIX the HEADING (drop the trailing emoji) and update the link; do "
            "NOT paste the invisible character into the link to make it "
            "resolve.\n" + "\n".join(
                f"  {f}:{n}  {frag!r}  {codes}" for f, n, frag, codes in offenders))
        assert scanned >= 200, (
            f"only {scanned} markdown files scanned — the walk did not reach the "
            "doc tree, so this assertion is vacuous")
