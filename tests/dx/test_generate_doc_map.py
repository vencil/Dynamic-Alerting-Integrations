"""Tests for generate_doc_map.py — docs/internal/doc-map.md generator.

Audit flagged 0% coverage. This is the CI drift gate (`--check` blocks
PRs with stale doc-map.md). Tests cover all helpers + gather_docs +
generate_doc_map + main() with REPO_ROOT monkeypatched to a temp tree.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import generate_doc_map as gdm  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Mount a tmp_path tree as REPO_ROOT.

    Yields the tmp_path so tests can populate docs/ etc. The
    DOC_MAP_ZH / DOC_MAP_EN module-level paths are also redirected so
    the writer / checker hit the temp tree.
    """
    monkeypatch.setattr(gdm, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        gdm, "DOC_MAP_ZH", tmp_path / "docs" / "internal" / "doc-map.md",
    )
    monkeypatch.setattr(
        gdm, "DOC_MAP_EN", tmp_path / "docs" / "internal" / "doc-map.en.md",
    )
    # Stub git ls-files so gather_docs's gitignore lookup doesn't shell out
    # to the host's real git repo.
    monkeypatch.setattr(
        gdm.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr="",
        ),
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "internal").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# _parse_front_matter — pure
# ---------------------------------------------------------------------------
class TestParseFrontMatter:
    def test_no_frontmatter_returns_empty(self):
        assert gdm._parse_front_matter("# Heading\nbody") == {}

    def test_unterminated_frontmatter_returns_empty(self):
        # Opens with --- but no closing line.
        assert gdm._parse_front_matter("---\ntitle: x\nstill open") == {}

    def test_basic_key_value(self):
        fm = gdm._parse_front_matter('---\ntitle: "Hello"\nlang: zh\n---\nbody')
        assert fm["title"] == "Hello"
        assert fm["lang"] == "zh"

    def test_strips_quotes(self):
        fm = gdm._parse_front_matter('---\ntitle: "Quoted"\nfoo: \'single\'\n---\n')
        assert fm["title"] == "Quoted"
        assert fm["foo"] == "single"

    def test_parses_list(self):
        fm = gdm._parse_front_matter(
            '---\naudience: [sre, devops, "ai-agent"]\n---\n'
        )
        assert fm["audience"] == ["sre", "devops", "ai-agent"]

    def test_skips_lines_without_colon(self):
        fm = gdm._parse_front_matter(
            "---\ntitle: x\nthis-line-has-no-colon\nlang: en\n---\n"
        )
        assert "title" in fm and "lang" in fm
        assert "this-line-has-no-colon" not in fm

    def test_empty_list_value(self):
        fm = gdm._parse_front_matter("---\naudience: []\n---\n")
        assert fm["audience"] == []


# ---------------------------------------------------------------------------
# _audience_str — pure
# ---------------------------------------------------------------------------
class TestAudienceStr:
    def test_empty_returns_all(self):
        assert gdm._audience_str([], "zh") == "All"

    def test_single_known_slug(self):
        assert gdm._audience_str(["sre"], "en") == "SREs"

    def test_multiple_slugs_joined(self):
        out = gdm._audience_str(["devops", "tenant"], "zh")
        assert "DevOps" in out
        assert "Tenants" in out
        assert ", " in out

    def test_unknown_slug_passes_through(self):
        # Unmapped slug returns the slug itself.
        assert gdm._audience_str(["mystery-role"], "zh") == "mystery-role"

    def test_lang_specific_translation(self):
        # "security" differs between zh and en.
        assert gdm._audience_str(["security"], "zh") == "安全合規"
        assert gdm._audience_str(["security"], "en") == "Security & Compliance"

    def test_unknown_lang_falls_back_to_zh(self):
        assert gdm._audience_str(["security"], "fr") == "安全合規"


# ---------------------------------------------------------------------------
# _has_en_pair — file existence
# ---------------------------------------------------------------------------
class TestHasEnPair:
    def test_true_when_pair_exists(self, tmp_path):
        zh = tmp_path / "guide.md"
        en = tmp_path / "guide.en.md"
        zh.write_text("zh", encoding="utf-8")
        en.write_text("en", encoding="utf-8")
        assert gdm._has_en_pair(zh) is True

    def test_false_when_pair_missing(self, tmp_path):
        zh = tmp_path / "guide.md"
        zh.write_text("zh", encoding="utf-8")
        assert gdm._has_en_pair(zh) is False

    def test_false_for_non_md_suffix(self, tmp_path):
        f = tmp_path / "thing.txt"
        f.write_text("x", encoding="utf-8")
        assert gdm._has_en_pair(f) is False


# ---------------------------------------------------------------------------
# _extract_h1_title — pure
# ---------------------------------------------------------------------------
class TestExtractH1Title:
    def test_finds_first_h1(self):
        assert gdm._extract_h1_title("# My Title\n## Sub\nbody") == "My Title"

    def test_strips_whitespace(self):
        assert gdm._extract_h1_title("   # Indented Title  \nbody") == "Indented Title"

    def test_no_h1_returns_empty(self):
        assert gdm._extract_h1_title("## H2 only\nbody") == ""

    def test_h2_alone_does_not_match(self):
        assert gdm._extract_h1_title("## Just H2\n### H3") == ""


# ---------------------------------------------------------------------------
# _get_map_path
# ---------------------------------------------------------------------------
class TestGetMapPath:
    def test_zh_returns_doc_map_zh(self):
        # Module-level constants apply when fixtures aren't engaged.
        assert gdm._get_map_path("zh") == gdm.DOC_MAP_ZH

    def test_en_returns_doc_map_en(self):
        assert gdm._get_map_path("en") == gdm.DOC_MAP_EN

    def test_unknown_lang_falls_back_to_zh(self):
        assert gdm._get_map_path("fr") == gdm.DOC_MAP_ZH


# ---------------------------------------------------------------------------
# gather_docs — composite
# ---------------------------------------------------------------------------
class TestGatherDocs:
    def test_empty_repo_returns_self_entries_only(self, fake_repo):
        entries = gdm.gather_docs("zh")
        # Self-entries are always appended. No real docs found.
        names = [e[0] for e in entries]
        assert any("doc-map.md" in n for n in names)
        # No content from docs/.
        assert not any("getting-started" in n.lower() for n in names)

    def test_picks_up_md_with_frontmatter(self, fake_repo):
        f = fake_repo / "docs" / "guide.md"
        f.write_text(
            '---\ntitle: "User Guide"\naudience: [sre, tenant]\n---\n# G\n',
            encoding="utf-8",
        )
        entries = gdm.gather_docs("zh")
        names = [e[0] for e in entries]
        assert any("guide.md" in n for n in names)
        # Find the entry to assert audience.
        guide = next(e for e in entries if "guide.md" in e[0])
        assert "SRE" in guide[1] or "Tenant" in guide[1]
        assert guide[2] == "User Guide"

    def test_falls_back_to_h1_when_no_frontmatter(self, fake_repo):
        f = fake_repo / "docs" / "raw.md"
        f.write_text("# Raw H1 Title\n\nbody\n", encoding="utf-8")
        entries = gdm.gather_docs("zh")
        raw = next(e for e in entries if "raw.md" in e[0])
        assert raw[2] == "Raw H1 Title"

    def test_falls_back_to_filename_when_no_title(self, fake_repo):
        f = fake_repo / "docs" / "no-h1-no-fm.md"
        f.write_text("plain body without heading", encoding="utf-8")
        entries = gdm.gather_docs("zh")
        entry = next(e for e in entries if "no-h1-no-fm.md" in e[0])
        # Stem.replace("-", " ").title() → "No H1 No Fm".
        assert entry[2] == "No H1 No Fm"

    def test_skips_underscore_prefix_files(self, fake_repo):
        (fake_repo / "docs" / "_resume-session.md").write_text(
            "# Internal\n", encoding="utf-8",
        )
        entries = gdm.gather_docs("zh")
        names = [e[0] for e in entries]
        assert not any("_resume-session" in n for n in names)

    def test_skips_internal_dir(self, fake_repo):
        # docs/internal/ is in SKIP_DIRS.
        (fake_repo / "docs" / "internal" / "playbook.md").write_text(
            "# Playbook\n", encoding="utf-8",
        )
        entries = gdm.gather_docs("zh")
        names = [e[0] for e in entries]
        assert not any("internal/playbook.md" in n for n in names)

    def test_skips_listed_skip_files(self, fake_repo):
        (fake_repo / "docs" / "tags.md").write_text("# Tags\n", encoding="utf-8")
        entries = gdm.gather_docs("zh")
        names = [e[0] for e in entries]
        assert not any(n.endswith("tags.md`") for n in names)

    def test_en_pair_marked_in_display(self, fake_repo):
        zh = fake_repo / "docs" / "bilingual.md"
        en = fake_repo / "docs" / "bilingual.en.md"
        zh.write_text("# ZH Title\n", encoding="utf-8")
        en.write_text(
            '---\ntitle: "EN Title"\n---\n# EN\n', encoding="utf-8",
        )
        entries = gdm.gather_docs("zh")
        bilingual = next(e for e in entries if "bilingual.md" in e[0])
        assert "(.en.md)" in bilingual[0]

    def test_lang_en_uses_en_frontmatter(self, fake_repo):
        zh = fake_repo / "docs" / "bilingual.md"
        en = fake_repo / "docs" / "bilingual.en.md"
        zh.write_text(
            '---\ntitle: "ZH"\n---\n# ZH\n', encoding="utf-8",
        )
        en.write_text(
            '---\ntitle: "EN Display"\n---\n# EN\n', encoding="utf-8",
        )
        entries = gdm.gather_docs("en")
        bilingual = next(e for e in entries if "bilingual.md" in e[0])
        assert bilingual[2] == "EN Display"

    def test_adr_excluded_by_default(self, fake_repo):
        adr = fake_repo / "docs" / "adr"
        adr.mkdir()
        (adr / "0001-decision.md").write_text(
            "# ADR-0001\n", encoding="utf-8",
        )
        entries = gdm.gather_docs("zh", include_adr=False)
        names = [e[0] for e in entries]
        assert not any("0001-decision" in n for n in names)

    def test_adr_included_when_flag_true(self, fake_repo):
        adr = fake_repo / "docs" / "adr"
        adr.mkdir()
        (adr / "0001-decision.md").write_text(
            "# ADR-0001\n", encoding="utf-8",
        )
        entries = gdm.gather_docs("zh", include_adr=True)
        names = [e[0] for e in entries]
        assert any("0001-decision" in n for n in names)

    def test_audience_string_treated_as_single_item_list(self, fake_repo):
        # Front matter with a single string audience (not list) gets wrapped.
        f = fake_repo / "docs" / "single.md"
        f.write_text(
            '---\ntitle: "Single"\naudience: sre\n---\n# H\n',
            encoding="utf-8",
        )
        entries = gdm.gather_docs("zh")
        single = next(e for e in entries if "single.md" in e[0])
        assert "SRE" in single[1]

    def test_self_entries_appended(self, fake_repo):
        entries = gdm.gather_docs("zh")
        # Last 2 entries are SELF_ENTRIES.
        last_two = entries[-2:]
        assert any("doc-map.md" in e[0] for e in last_two)
        assert any("tool-map.md" in e[0] for e in last_two)


# ---------------------------------------------------------------------------
# generate_doc_map — formatter
# ---------------------------------------------------------------------------
class TestGenerateDocMap:
    def test_zh_header_format(self, fake_repo):
        out = gdm.generate_doc_map("zh")
        assert "title: \"文件導覽" in out
        assert "lang: zh" in out
        assert "| 文件 | 受眾 | 內容 |" in out

    def test_en_header_format(self, fake_repo):
        out = gdm.generate_doc_map("en")
        assert "title: \"Documentation Map\"" in out
        assert "lang: en" in out
        assert "| File | Audience | Description |" in out

    def test_includes_doc_entries(self, fake_repo):
        f = fake_repo / "docs" / "guide.md"
        f.write_text(
            '---\ntitle: "Guide"\naudience: [sre]\n---\n# G\n',
            encoding="utf-8",
        )
        out = gdm.generate_doc_map("zh")
        assert "guide.md" in out
        assert "Guide" in out

    def test_includes_extra_entries(self, fake_repo):
        out = gdm.generate_doc_map("zh")
        # EXTRA_ENTRIES contains tenant-config.schema.json reference.
        assert "tenant-config.schema.json" in out


# ---------------------------------------------------------------------------
# main — CLI
# ---------------------------------------------------------------------------
class TestMain:
    def test_default_prints_to_stdout(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["generate_doc_map.py"])
        gdm.main()
        out = capsys.readouterr().out
        assert "文件導覽" in out  # zh default

    def test_lang_en_prints_english_map(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--lang", "en"])
        gdm.main()
        out = capsys.readouterr().out
        assert "Documentation Map" in out
        assert "文件導覽" not in out

    def test_generate_writes_zh_file(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--generate"])
        gdm.main()
        out = capsys.readouterr().out
        assert "Generated" in out
        assert (fake_repo / "docs" / "internal" / "doc-map.md").exists()

    def test_generate_lang_all_writes_both(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--generate", "--lang", "all"])
        gdm.main()
        zh_path = fake_repo / "docs" / "internal" / "doc-map.md"
        en_path = fake_repo / "docs" / "internal" / "doc-map.en.md"
        assert zh_path.exists() and en_path.exists()

    def test_check_clean_returns_zero(self, fake_repo, monkeypatch, capsys):
        # Generate first, then check — must be clean.
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--generate"])
        gdm.main()
        capsys.readouterr()  # clear output
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--check"])
        gdm.main()  # should not exit
        out = capsys.readouterr().out
        assert "up to date" in out

    def test_check_missing_file_exits_one(self, fake_repo, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--check"])
        with pytest.raises(SystemExit) as exc:
            gdm.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "does not exist" in out

    def test_check_outdated_file_exits_one(self, fake_repo, monkeypatch, capsys):
        # Write a placeholder that doesn't match the generated content.
        (fake_repo / "docs" / "internal" / "doc-map.md").write_text(
            "stale content", encoding="utf-8",
        )
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--check"])
        with pytest.raises(SystemExit) as exc:
            gdm.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "outdated" in out

    def test_check_diff_lists_missing_extra(self, fake_repo, monkeypatch, capsys):
        # Generate once with one doc.
        f1 = fake_repo / "docs" / "first.md"
        f1.write_text("# First\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--generate"])
        gdm.main()
        capsys.readouterr()
        # Add a new doc; existing doc-map.md is now stale (missing the new doc).
        f2 = fake_repo / "docs" / "second.md"
        f2.write_text("# Second\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--check"])
        with pytest.raises(SystemExit):
            gdm.main()
        out = capsys.readouterr().out
        assert "missing" in out
        assert "second.md" in out

    def test_no_adr_overrides_include_adr_default(
        self, fake_repo, monkeypatch, capsys,
    ):
        adr = fake_repo / "docs" / "adr"
        adr.mkdir()
        (adr / "0001-x.md").write_text("# ADR\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--no-adr"])
        gdm.main()
        out = capsys.readouterr().out
        assert "0001-x" not in out

    def test_safe_flag_uses_atomic_write(self, fake_repo, monkeypatch, capsys):
        called = {"atomic": False, "regular": False}

        def fake_atomic(path, content, newline=None):
            called["atomic"] = True
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
        monkeypatch.setattr(gdm, "atomic_write_text", fake_atomic)
        monkeypatch.setattr(sys, "argv",
                            ["generate_doc_map.py", "--generate", "--safe"])
        gdm.main()
        assert called["atomic"] is True
