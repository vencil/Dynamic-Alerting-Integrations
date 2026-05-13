"""Tests for scripts/dx/generate_adr_index.py — ADR index drift gate.

Pinned contracts
----------------
1. **Discovery**:
   - Only `NNN-kebab-case.md` ZH files match; `*.en.md` siblings excluded.
   - `README.md`, dotfiles, non-conforming names ignored silently.

2. **Frontmatter parsing**:
   - `title:` is required (raises AdrParseError if missing).
   - `ADR-NNN: ` / `ADR-NNN：` prefixes are stripped from the title.

3. **Status H2 parsing** (covers all 3 in-tree formats):
   - ASCII parens: `✅ **Accepted** (v1.0.0)` → ("✅", "Accepted", "v1.0.0")
   - CJK parens:   `✅ **Accepted**（v2.7.0, 2026-04-16）— ...` → emoji + name + version
   - PR-prefixed:  `✅ **Accepted**（PR [#375](url)，v2.8.0）` → version still extracted
   - Alt emoji:    `🟢 **Accepted**` and `🟡 **Proposed**` accepted

4. **Sentinel replacement**:
   - Idempotent (running --write twice leaves the file identical).
   - Drift gate: `--check` returns 1 when the rendered table differs from disk.
   - Missing sentinel → ValueError with actionable message.

5. **Real-corpus integration**:
   - The generator parses every shipped `docs/adr/[0-9]*-*.md` without error.
   - `--check` against the live target doc passes (post-merge consumer expectation).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_DX_DIR = REPO_ROOT / "scripts" / "dx"
sys.path.insert(0, str(_DX_DIR))

import generate_adr_index as gai  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_adr(path: Path, *, title: str, status_block: str) -> None:
    """Write a synthetic ADR with the minimum frontmatter the generator needs."""
    text = (
        "---\n"
        f'title: "{title}"\n'
        "tags: [adr, test]\n"
        "audience: [platform-engineers]\n"
        "version: v9.9.9\n"
        "lang: zh\n"
        "---\n"
        "\n"
        f"# {title}\n"
        "\n"
        "## 狀態\n"
        "\n"
        f"{status_block}\n"
        "\n"
        "## 背景\n"
        "\n"
        "test body\n"
    )
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_adr — covers all 3 in-tree status formats
# ---------------------------------------------------------------------------
class TestParseAdr:
    def test_ascii_parens(self, tmp_path):
        f = tmp_path / "001-foo-bar.md"
        _write_adr(f, title="ADR-001: Foo Bar", status_block="✅ **Accepted** (v1.0.0)")
        e = gai.parse_adr(f)
        assert e.number == "001"
        assert e.title == "Foo Bar"
        assert e.status_emoji == "✅"
        assert e.status_name == "Accepted"
        assert e.version == "v1.0.0"
        assert e.rel_path == "adr/001-foo-bar.md"

    def test_cjk_parens_with_date(self, tmp_path):
        f = tmp_path / "014-budget.md"
        _write_adr(
            f,
            title="ADR-014: Budget Isolation",
            status_block="✅ **Accepted**（v2.7.0, 2026-04-16）— 已 land",
        )
        e = gai.parse_adr(f)
        assert e.status_emoji == "✅"
        assert e.status_name == "Accepted"
        assert e.version == "v2.7.0"

    def test_pr_prefixed_version(self, tmp_path):
        f = tmp_path / "020-ssot.md"
        _write_adr(
            f,
            title="ADR-020: Planning SSOT",
            status_block="✅ **Accepted**（PR [#375](https://example/pr/375)，v2.8.0）",
        )
        e = gai.parse_adr(f)
        assert e.version == "v2.8.0"  # not v0.0.0 from #375

    def test_alt_emoji_green(self, tmp_path):
        f = tmp_path / "019-profile.md"
        _write_adr(f, title="ADR-019: Profile Default", status_block="🟢 **Accepted** (v2.8.0)")
        assert gai.parse_adr(f).status_emoji == "🟢"

    def test_proposed_status(self, tmp_path):
        f = tmp_path / "021-federation.md"
        _write_adr(f, title="ADR-021: Federation", status_block="🟡 **Proposed** (v2.8.0)")
        e = gai.parse_adr(f)
        assert e.status_emoji == "🟡"
        assert e.status_name == "Proposed"

    def test_full_width_colon_in_title(self, tmp_path):
        f = tmp_path / "999-foo.md"
        _write_adr(f, title="ADR-999：中文標題", status_block="✅ **Accepted** (v1.0.0)")
        assert gai.parse_adr(f).title == "中文標題"

    def test_missing_frontmatter(self, tmp_path):
        f = tmp_path / "001-foo.md"
        f.write_text("# No Frontmatter\n## 狀態\n✅ **Accepted** (v1.0.0)\n", encoding="utf-8")
        with pytest.raises(gai.AdrParseError, match="missing YAML frontmatter"):
            gai.parse_adr(f)

    def test_missing_title(self, tmp_path):
        f = tmp_path / "001-foo.md"
        f.write_text("---\nlang: zh\n---\n## 狀態\n✅ **Accepted** (v1.0.0)\n", encoding="utf-8")
        with pytest.raises(gai.AdrParseError, match="`title` missing"):
            gai.parse_adr(f)

    def test_missing_status_section(self, tmp_path):
        f = tmp_path / "001-foo.md"
        f.write_text(
            '---\ntitle: "ADR-001: Foo"\nlang: zh\n---\n# Foo\n## 背景\nno status here\n',
            encoding="utf-8",
        )
        with pytest.raises(gai.AdrParseError, match="missing `## 狀態`"):
            gai.parse_adr(f)

    def test_status_section_without_emoji_pattern(self, tmp_path):
        f = tmp_path / "001-foo.md"
        _write_adr(f, title="ADR-001: Foo", status_block="just plain text without the emoji-bold pattern")
        with pytest.raises(gai.AdrParseError, match="no `<emoji> "):
            gai.parse_adr(f)

    def test_no_version_in_status_keeps_blank(self, tmp_path):
        f = tmp_path / "001-foo.md"
        _write_adr(f, title="ADR-001: Foo", status_block="🟡 **Proposed** — version not set yet")
        assert gai.parse_adr(f).version == ""

    def test_filename_pattern_enforced(self, tmp_path):
        f = tmp_path / "Misc-not-a-number.md"
        _write_adr(f, title="ADR-001: Foo", status_block="✅ **Accepted** (v1.0.0)")
        with pytest.raises(gai.AdrParseError, match="NNN-kebab-case"):
            gai.parse_adr(f)


# ---------------------------------------------------------------------------
# discover_adrs — exclusion rules
# ---------------------------------------------------------------------------
class TestDiscoverAdrs:
    def test_excludes_en_siblings(self, tmp_path):
        for name in ("001-foo.md", "001-foo.en.md", "002-bar.md", "002-bar.en.md"):
            (tmp_path / name).write_text("dummy", encoding="utf-8")
        files = gai.discover_adrs(tmp_path)
        assert [f.name for f in files] == ["001-foo.md", "002-bar.md"]

    def test_excludes_readme_and_dotfiles(self, tmp_path):
        (tmp_path / "001-foo.md").write_text("dummy", encoding="utf-8")
        (tmp_path / "README.md").write_text("dummy", encoding="utf-8")
        (tmp_path / ".keep").write_text("dummy", encoding="utf-8")
        (tmp_path / "draft.md").write_text("dummy", encoding="utf-8")
        files = gai.discover_adrs(tmp_path)
        assert [f.name for f in files] == ["001-foo.md"]

    def test_sorted_by_number(self, tmp_path):
        for name in ("020-ssot.md", "001-foo.md", "010-mid.md"):
            (tmp_path / name).write_text("dummy", encoding="utf-8")
        files = gai.discover_adrs(tmp_path)
        assert [f.name for f in files] == ["001-foo.md", "010-mid.md", "020-ssot.md"]


# ---------------------------------------------------------------------------
# render_table — golden output
# ---------------------------------------------------------------------------
class TestRenderTable:
    def test_two_entries_golden(self):
        entries = [
            gai.AdrEntry("001", "Foo Bar", "✅", "Accepted", "v1.0.0", "adr/001-foo-bar.md"),
            gai.AdrEntry("021", "Baz", "🟡", "Proposed", "v2.8.0", "adr/021-baz.md"),
        ]
        out = gai.render_table(entries)
        assert out == (
            "| ADR | 標題 | 狀態 | 版本 |\n"
            "|-----|------|------|------|\n"
            "| ADR-001 | [Foo Bar](adr/001-foo-bar.md) | ✅ Accepted | v1.0.0 |\n"
            "| ADR-021 | [Baz](adr/021-baz.md) | 🟡 Proposed | v2.8.0 |\n"
        )

    def test_blank_version_em_dash(self):
        entries = [gai.AdrEntry("001", "Foo", "🟡", "Proposed", "", "adr/001-foo.md")]
        out = gai.render_table(entries)
        assert "| 🟡 Proposed | — |" in out


# ---------------------------------------------------------------------------
# replace_sentinel_block — wrap & idempotency
# ---------------------------------------------------------------------------
class TestReplaceSentinelBlock:
    def test_replaces_existing_block(self):
        content = (
            "intro\n"
            f"{gai.SENTINEL_START}\n"
            "old garbage table\n"
            f"{gai.SENTINEL_END}\n"
            "outro\n"
        )
        new = gai.replace_sentinel_block(content, "NEW TABLE\n")
        assert gai.SENTINEL_START in new
        assert gai.SENTINEL_END in new
        assert "NEW TABLE" in new
        assert "old garbage" not in new

    def test_missing_sentinel_raises(self):
        with pytest.raises(ValueError, match="Sentinel block missing"):
            gai.replace_sentinel_block("no markers here", "x\n")

    def test_idempotent(self):
        content = (
            f"prefix\n{gai.SENTINEL_START}\nold\n{gai.SENTINEL_END}\nsuffix\n"
        )
        once = gai.replace_sentinel_block(content, "fresh\n")
        twice = gai.replace_sentinel_block(once, "fresh\n")
        assert once == twice


# ---------------------------------------------------------------------------
# main() — end-to-end via subprocess (drift gate behavior)
# ---------------------------------------------------------------------------
class TestMainCli:
    @pytest.fixture
    def tiny_repo(self, tmp_path):
        """Stand up a tiny repo with 2 ADRs + a target doc with sentinels."""
        adr_dir = tmp_path / "adr"
        adr_dir.mkdir()
        _write_adr(
            adr_dir / "001-alpha.md",
            title="ADR-001: Alpha",
            status_block="✅ **Accepted** (v1.0.0)",
        )
        _write_adr(
            adr_dir / "002-beta.md",
            title="ADR-002: Beta",
            status_block="🟡 **Proposed** (v2.0.0)",
        )
        target = tmp_path / "arch.md"
        target.write_text(
            "# Hub\n\n"
            f"{gai.SENTINEL_START}\n"
            "stale stuff\n"
            f"{gai.SENTINEL_END}\n",
            encoding="utf-8",
        )
        return adr_dir, target

    def _run(self, *args, expect_returncode=0):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [sys.executable, str(_DX_DIR / "generate_adr_index.py"), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=30,
        )
        assert proc.returncode == expect_returncode, (
            f"exit={proc.returncode}\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )
        return proc

    def test_check_drift_exit_1(self, tiny_repo):
        adr_dir, target = tiny_repo
        proc = self._run(
            "--check", "--adr-dir", str(adr_dir), "--target", str(target),
            expect_returncode=1,
        )
        assert "DRIFT" in proc.stderr
        assert "make adr-index" in proc.stderr

    def test_write_then_check_clean(self, tiny_repo):
        adr_dir, target = tiny_repo
        self._run("--write", "--adr-dir", str(adr_dir), "--target", str(target))
        body = target.read_text(encoding="utf-8")
        assert "ADR-001" in body and "Alpha" in body
        assert "ADR-002" in body and "🟡 Proposed" in body
        # --check on freshly written file should pass.
        proc = self._run(
            "--check", "--adr-dir", str(adr_dir), "--target", str(target),
        )
        assert "OK" in proc.stdout

    def test_write_is_idempotent(self, tiny_repo):
        adr_dir, target = tiny_repo
        self._run("--write", "--adr-dir", str(adr_dir), "--target", str(target))
        once = target.read_text(encoding="utf-8")
        proc = self._run(
            "--write", "--adr-dir", str(adr_dir), "--target", str(target),
        )
        assert "no change" in proc.stdout
        assert target.read_text(encoding="utf-8") == once

    def test_write_forces_lf_newlines(self, tiny_repo):
        """Regen output must be LF on every platform — see PR #475 self-review
        issue A. Without atomic_write_text(newline='\\n'), Path.write_text on
        Windows translates '\\n' into '\\r\\n' and the same regen produces
        byte-different output across hosts; `.gitattributes eol=lf` masks the
        diff at commit time but the working-copy `--check` step still drifts.
        """
        adr_dir, target = tiny_repo
        self._run("--write", "--adr-dir", str(adr_dir), "--target", str(target))
        # Read in binary mode to bypass Python's universal-newlines translation;
        # we want to see exactly what bytes hit disk.
        raw = target.read_bytes()
        assert b"\r\n" not in raw, (
            "Regenerated file contains CRLF — atomic_write_text(newline='\\n') "
            "is supposed to force LF on every platform."
        )
        assert raw.endswith(b"\n"), "File should terminate with a final LF."

    def test_mode_flag_required(self, tiny_repo):
        adr_dir, target = tiny_repo
        # Neither --check nor --write → argparse exit code 2.
        proc = self._run(
            "--adr-dir", str(adr_dir), "--target", str(target),
            expect_returncode=2,
        )
        assert "one of the arguments" in proc.stderr or "required" in proc.stderr


# ---------------------------------------------------------------------------
# Real-corpus integration — guards the live drift gate
# ---------------------------------------------------------------------------
class TestRealCorpus:
    def test_all_shipped_adrs_parse(self):
        files = gai.discover_adrs()
        assert len(files) >= 21, "expected at least 21 ZH ADRs in docs/adr/"
        for f in files:
            entry = gai.parse_adr(f)
            assert entry.title, f"{f.name}: empty title"
            assert entry.status_emoji, f"{f.name}: empty emoji"
            assert entry.status_name, f"{f.name}: empty status name"

    def test_live_target_doc_in_sync(self):
        """If this fails, run `make adr-index` to regenerate the table."""
        adr_files = gai.discover_adrs()
        entries = [gai.parse_adr(p) for p in adr_files]
        table = gai.render_table(entries)
        current = gai.TARGET_DOC.read_text(encoding="utf-8")
        new = gai.replace_sentinel_block(current, table)
        assert new == current, (
            "ADR index in docs/architecture-and-design.md is stale. "
            "Run `make adr-index` to refresh."
        )
