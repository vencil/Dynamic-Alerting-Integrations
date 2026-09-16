"""Tests for generate_changelog.py — Conventional commit parsing and CHANGELOG generation.

Merged from previous _extra split (PR test-refactor sweep): pure parser/regex/
constant/format coverage sits alongside git_cmd / get_latest_tag /
get_commits_since / load_ignored_commits / lint_changelog / main() classes
appended below.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

import generate_changelog as gc
from _lib_exitcodes import EXIT_CALLER_ERROR


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """subprocess.CompletedProcess fixture for monkeypatched git_cmd tests."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# parse_commit
# ---------------------------------------------------------------------------
class TestParseCommit:
    """Tests for parse_commit() — conventional commit parsing."""

    def test_simple_feat(self):
        result = gc.parse_commit("feat: add shadow monitoring support")
        assert result is not None
        assert result["type"] == "feat"
        assert result["scope"] == ""
        assert result["breaking"] is False
        assert result["desc"] == "add shadow monitoring support"

    def test_scoped_fix(self):
        result = gc.parse_commit("fix(exporter): resolve HA race condition")
        assert result is not None
        assert result["type"] == "fix"
        assert result["scope"] == "exporter"
        assert result["breaking"] is False

    def test_breaking_change(self):
        result = gc.parse_commit("feat!: remove deprecated v1 API")
        assert result is not None
        assert result["breaking"] is True
        assert result["type"] == "feat"

    def test_scoped_breaking(self):
        result = gc.parse_commit("refactor(config)!: rename _severity_dedup to inhibit-based")
        assert result is not None
        assert result["type"] == "refactor"
        assert result["scope"] == "config"
        assert result["breaking"] is True

    def test_docs_type(self):
        result = gc.parse_commit("docs: update architecture-and-design.md")
        assert result is not None
        assert result["type"] == "docs"

    def test_all_valid_types(self):
        for commit_type in gc.TYPE_SECTIONS:
            result = gc.parse_commit(f"{commit_type}: test message")
            assert result is not None, f"Failed to parse type: {commit_type}"
            assert result["type"] == commit_type

    def test_invalid_no_colon(self):
        result = gc.parse_commit("just a regular commit message")
        assert result is None

    def test_invalid_uppercase_type(self):
        result = gc.parse_commit("FEAT: uppercase type not conventional")
        assert result is None

    def test_empty_string(self):
        result = gc.parse_commit("")
        assert result is None


# ---------------------------------------------------------------------------
# COMMIT_RE regex
# ---------------------------------------------------------------------------
class TestCommitRegex:
    """Tests for the COMMIT_RE regex pattern."""

    def test_captures_all_groups(self):
        m = gc.COMMIT_RE.match("feat(scope)!: description")
        assert m is not None
        assert m.group("type") == "feat"
        assert m.group("scope") == "scope"
        assert m.group("breaking") == "!"
        assert m.group("desc") == "description"

    def test_scope_with_hyphen(self):
        m = gc.COMMIT_RE.match("fix(rule-pack): fix yaml parsing")
        assert m is not None
        assert m.group("scope") == "rule-pack"

    def test_scope_with_slash(self):
        m = gc.COMMIT_RE.match("feat(ops/diagnose): add profile lookup")
        assert m is not None
        assert m.group("scope") == "ops/diagnose"


# ---------------------------------------------------------------------------
# TYPE_SECTIONS / TYPE_EMOJI
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate constant dictionaries."""

    def test_type_sections_covers_all_standard_types(self):
        standard = {"feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "style", "revert"}
        assert standard.issubset(set(gc.TYPE_SECTIONS.keys()))

    def test_type_emoji_subset_of_sections(self):
        for key in gc.TYPE_EMOJI:
            assert key in gc.TYPE_SECTIONS, f"Emoji key '{key}' not in TYPE_SECTIONS"


# ---------------------------------------------------------------------------
# format_changelog
# ---------------------------------------------------------------------------
class TestFormatChangelog:
    """Tests for format_changelog() — takes grouped dict + breaking list."""

    def test_basic_formatting(self):
        grouped = {
            "feat": [{"scope": "", "desc": "add feature A"}],
            "fix": [{"scope": "exporter", "desc": "fix bug B"}],
        }
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=[])
        assert "v2.1.0" in result
        assert "Features" in result
        assert "add feature A" in result
        assert "fix bug B" in result

    def test_empty_commits(self):
        result = gc.format_changelog({}, version="v2.1.0", breaking=[])
        assert "v2.1.0" in result

    def test_breaking_changes_highlighted(self):
        grouped = {"feat": [{"scope": "", "desc": "remove old API"}]}
        breaking = [{"scope": "", "desc": "remove old API"}]
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=breaking)
        assert "Breaking" in result
        assert "remove old API" in result

    def test_scoped_commits_grouped(self):
        grouped = {
            "fix": [
                {"scope": "exporter", "desc": "fix race condition"},
                {"scope": "exporter", "desc": "fix reload"},
                {"scope": "", "desc": "fix typo"},
            ],
        }
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=[])
        assert "exporter" in result
        assert "fix race condition" in result


# ---------------------------------------------------------------------------
# Orchestrator + git/IO + lint coverage (was test_generate_changelog_extra.py)
#
# Audit flagged 28.5% coverage. The tests below fill the remaining surface:
#   - git_cmd (subprocess wrapper, success + error)
#   - get_latest_tag (git describe with optional fallback)
#   - get_commits_since (git log + parse)
#   - load_ignored_commits (file-based ignore list)
#   - lint_changelog (full markdown lint)
#   - main() (default / --check / --lint / --output / --since)
# ---------------------------------------------------------------------------


class TestGitCmd:
    def test_success_returns_stripped_stdout(self, monkeypatch):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(0, "  hello\n", ""))
        assert gc.git_cmd(["log"]) == "hello"

    def test_failure_exits_caller_error(self, monkeypatch, capsys):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(128, "", "fatal: not a repo"))
        with pytest.raises(SystemExit) as exc:
            gc.git_cmd(["log"])
        assert exc.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "git log failed" in err
        assert "not a repo" in err


class TestGetLatestTag:
    def test_success_returns_tag(self, monkeypatch):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(0, "v2.7.0\n", ""))
        assert gc.get_latest_tag() == "v2.7.0"

    def test_no_tags_returns_none(self, monkeypatch):
        # git describe exits non-zero when no tags exist.
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(128, "", "no tags"))
        assert gc.get_latest_tag() is None

    def test_oserror_returns_none(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("git not found")
        monkeypatch.setattr(gc.subprocess, "run", boom)
        assert gc.get_latest_tag() is None

    def test_subprocess_error_returns_none(self, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.SubprocessError("oops")
        monkeypatch.setattr(gc.subprocess, "run", boom)
        assert gc.get_latest_tag() is None


class TestGetCommitsSince:
    def test_with_since_ref(self, monkeypatch):
        log = "abcdef123456|feat: add x\n0011223344ff|fix(ci): typo"
        monkeypatch.setattr(gc, "git_cmd", lambda args: log)
        commits = gc.get_commits_since("v2.0.0")
        assert commits == [
            ("abcdef123456", "feat: add x"),
            ("0011223344ff", "fix(ci): typo"),
        ]

    def test_without_since_ref_uses_head(self, monkeypatch):
        captured = {}

        def fake_git_cmd(args):
            captured["args"] = args
            return ""
        monkeypatch.setattr(gc, "git_cmd", fake_git_cmd)
        gc.get_commits_since(None)
        # No since → log_range "HEAD".
        assert "HEAD" in captured["args"]
        assert ".." not in " ".join(captured["args"])

    def test_empty_log_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(gc, "git_cmd", lambda args: "")
        assert gc.get_commits_since("v1") == []

    def test_truncates_hash_to_12_chars(self, monkeypatch):
        long_hash = "0123456789abcdef0123456789abcdef01234567"
        monkeypatch.setattr(gc, "git_cmd", lambda args: f"{long_hash}|feat: x")
        commits = gc.get_commits_since(None)
        assert commits == [("0123456789ab", "feat: x")]

    def test_lines_without_pipe_silently_skipped(self, monkeypatch):
        # Defensive parse: garbage lines without `|` separator are dropped.
        log = "abc123|feat: ok\nno-separator-line\ndef456|fix: also-ok"
        monkeypatch.setattr(gc, "git_cmd", lambda args: log)
        commits = gc.get_commits_since(None)
        assert len(commits) == 2
        assert commits[0][1] == "feat: ok"


class TestLoadIgnoredCommits:
    def _stub_repo_root(self, monkeypatch, root: str):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(0, root + "\n", ""))

    def test_no_ignore_file_returns_empty(self, monkeypatch, tmp_path):
        self._stub_repo_root(monkeypatch, str(tmp_path))
        assert gc.load_ignored_commits() == []

    def test_repo_root_lookup_fails_returns_empty(self, monkeypatch):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(128, "", "not a repo"))
        assert gc.load_ignored_commits() == []

    def test_oserror_returns_empty(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("denied")
        monkeypatch.setattr(gc.subprocess, "run", boom)
        assert gc.load_ignored_commits() == []

    def test_parses_prefixes_lowercased(self, monkeypatch, tmp_path):
        ignore = tmp_path / ".changelog-lint-ignore"
        ignore.write_text(
            "# leading comment line\n"
            "\n"  # blank line
            "ABCDEF123456 trailing comment is ok\n"
            "  # indented comment\n"
            "FFFF0000\n",
            encoding="utf-8",
        )
        self._stub_repo_root(monkeypatch, str(tmp_path))
        prefixes = gc.load_ignored_commits()
        assert prefixes == ["abcdef123456", "ffff0000"]

    def test_io_error_during_read_returns_empty(self, monkeypatch, tmp_path):
        # Create directory-as-file to trigger OSError on open.
        ignore = tmp_path / ".changelog-lint-ignore"
        ignore.mkdir()  # opens as file → IsADirectoryError (subclass of OSError)
        self._stub_repo_root(monkeypatch, str(tmp_path))
        assert gc.load_ignored_commits() == []


class TestLintChangelog:
    def _make(self, tmp_path: Path, content: str) -> Path:
        f = tmp_path / "CHANGELOG.md"
        f.write_text(content, encoding="utf-8")
        return f

    def test_missing_file_reports_issue(self, tmp_path):
        ghost = tmp_path / "GHOST.md"
        issues = gc.lint_changelog(str(ghost))
        assert len(issues) == 1
        assert "not found" in issues[0]

    def test_clean_changelog_returns_no_issues(self, tmp_path):
        f = self._make(tmp_path, (
            "## [v2.8.0] — Title (2026-05-07)\n"
            "\n"
            "### ✨ Features\n"
            "- something\n"
            "\n"
            "## [v2.7.0] — Earlier (2026-04-01)\n"
            "\n"
            "### 🐛 Bug Fixes\n"
            "- fixed it\n"
        ))
        assert gc.lint_changelog(str(f)) == []

    def test_missing_date_flagged(self, tmp_path):
        f = self._make(tmp_path, (
            "## [v2.8.0] — Title\n"
            "\n"
            "### Features\n"
            "- x\n"
        ))
        issues = gc.lint_changelog(str(f))
        assert any("missing date" in i for i in issues)

    def test_no_subsection_flagged(self, tmp_path):
        f = self._make(tmp_path, (
            "## [v2.8.0] — Title (2026-05-07)\n"
            "\n"
            "Just a paragraph, no ### section.\n"
        ))
        issues = gc.lint_changelog(str(f))
        assert any("no ### subsections" in i for i in issues)

    def test_duplicate_version_flagged(self, tmp_path):
        f = self._make(tmp_path, (
            "## [v2.8.0] — First (2026-05-07)\n\n### X\n- a\n\n"
            "## [v2.8.0] — Dup (2026-05-08)\n\n### Y\n- b\n"
        ))
        issues = gc.lint_changelog(str(f))
        assert any("duplicate version" in i for i in issues)

    def test_last_version_no_subsection_caught(self, tmp_path):
        # The "current_version + has_subsection check" runs once at the
        # next version header AND once at EOF — make sure the EOF case
        # catches the trailing version.
        f = self._make(tmp_path, (
            "## [v2.8.0] — Earlier (2026-05-01)\n\n### X\n- a\n\n"
            "## [v2.9.0] — Last (2026-05-07)\n"  # no ### below
        ))
        issues = gc.lint_changelog(str(f))
        assert any("[2.9.0]" in i and "no ### subsections" in i for i in issues)


class TestLintSeesTheUnreleasedSection:
    """#1765 — the one section every PR edits used to be invisible.

    The header pattern was semver-only and every check was gated on having
    seen a version, so nothing above the first released heading was linted.
    """

    def _make(self, tmp_path: Path, content: str) -> Path:
        f = tmp_path / "CHANGELOG.md"
        f.write_text(content, encoding="utf-8")
        return f

    _RELEASED = "## [v2.9.0] — Title (2026-06-06)\n\n### Fixed\n- a\n"

    def test_unreleased_without_subsections_is_flagged(self, tmp_path):
        f = self._make(tmp_path, "## [Unreleased]\n\nJust prose.\n\n" + self._RELEASED)
        issues = gc.lint_changelog(str(f))
        assert any("[Unreleased]" in i and "no ### subsections" in i for i in issues), issues

    def test_a_second_unreleased_heading_is_flagged(self, tmp_path):
        f = self._make(tmp_path,
                       "## [Unreleased]\n\n### Fixed\n- a\n\n"
                       "## [Unreleased]\n\n### Fixed\n- b\n\n" + self._RELEASED)
        issues = gc.lint_changelog(str(f))
        assert any("duplicate version [Unreleased]" in i for i in issues), issues

    def test_unreleased_needs_no_date(self, tmp_path):
        """⛔ Demanding one would make the common case permanently red, which
        is how a gate gets switched off."""
        f = self._make(tmp_path, "## [Unreleased]\n\n### Fixed\n- a\n\n" + self._RELEASED)
        assert gc.lint_changelog(str(f)) == []

    def test_a_repeated_subsection_name_is_not_an_issue(self, tmp_path):
        """Each PR prepends its own block, so a second `### Fixed` under
        Unreleased is the convention working, not a defect."""
        f = self._make(tmp_path,
                       "## [Unreleased]\n\n### Fixed\n- a\n\n### Fixed\n- b\n\n"
                       + self._RELEASED)
        assert gc.lint_changelog(str(f)) == []

    def test_released_sections_still_need_a_date(self, tmp_path):
        """必響對照組 for the exemption above: without it, the exemption could
        be implemented as 'never check dates' and nothing would notice."""
        f = self._make(tmp_path, "## [Unreleased]\n\n### Fixed\n- a\n\n"
                                 "## [v2.9.0] — Title\n\n### Fixed\n- b\n")
        issues = gc.lint_changelog(str(f))
        assert any("missing date" in i for i in issues), issues


class TestLintCannotBeSilencedByTheFileItself:
    """Blind review found three ways the file could switch the lint off."""

    def _make(self, tmp_path: Path, content: str) -> Path:
        f = tmp_path / "CHANGELOG.md"
        f.write_text(content, encoding="utf-8")
        return f

    @pytest.mark.parametrize("heading", [
        "## [Unrelesed]",   # typo — was silently ignored
        "## [2.10.0] — T (2026-09-08)",  # missing `v`; bump_docs requires it
    ], ids=["typo", "no-v-prefix"])
    def test_an_unrecognised_heading_is_named(self, tmp_path, heading):
        """Every check is gated on having entered a section, so an
        unrecognised heading does not fail — it makes its whole section
        invisible. That is the ticket's own defect, reachable by one typo."""
        f = self._make(tmp_path, heading + "\n\n- content, no subsection\n")
        issues = gc.lint_changelog(str(f))
        assert any("not a recognised section heading" in i for i in issues), issues

    def test_a_double_spaced_unreleased_heading_is_named(self, tmp_path):
        """`##  [Unreleased]` (two spaces) matched neither the semver pattern
        nor the old `## [` heading pattern, so the section vanished from this
        lint AND the entry cap with no output (blind review, PR-C)."""
        f = self._make(tmp_path, "##  [Unreleased]\n\n### Fixed\n- a\n")
        issues = gc.lint_changelog(str(f))
        assert any("not a recognised section heading" in i for i in issues), issues

    def test_a_fenced_heading_is_not_real_structure(self, tmp_path):
        """An entry that DOCUMENTS changelog markup must not be read as
        markup. Both directions: no phantom duplicate from the fence…"""
        f = self._make(tmp_path,
                       "## [Unreleased]\n\n### Fixed\n- a\n\n"
                       "```markdown\n## [Unreleased]\n### Fixed\n```\n")
        assert gc.lint_changelog(str(f)) == []

    def test_a_fenced_subsection_does_not_satisfy_a_section(self, tmp_path):
        """…and no phantom satisfaction either."""
        f = self._make(tmp_path,
                       "## [v2.9.0] — T (2026-06-06)\n\n- prose only\n\n"
                       "```markdown\n### Fixed\n```\n")
        issues = gc.lint_changelog(str(f))
        assert any("no ### subsections" in i for i in issues), issues

    def test_an_empty_unreleased_placeholder_is_allowed(self, tmp_path):
        """The shape release wrap-up leaves behind after cutting Unreleased
        into `## [vX.Y.Z]`. Flagging it would make the gate red on the release
        commit itself — which is how a gate gets switched off."""
        f = self._make(tmp_path,
                       "## [Unreleased]\n\n<!-- next release goes here -->\n\n"
                       "## [v2.9.0] — T (2026-06-06)\n\n### Fixed\n- a\n")
        assert gc.lint_changelog(str(f)) == []


class TestLintCliSeparatesCallerErrorFromFinding:
    """`_lib_exitcodes`: a bad path or an unreadable file is rc 2, not rc 1.

    An uncaught traceback also exits 1, so without this a caller cannot tell
    "your changelog has one issue" from "I could not read it".
    """

    def test_a_missing_path_is_a_caller_error(self, tmp_path, cli_argv, capsys):
        cli_argv("generate_changelog.py", "--lint", str(tmp_path / "nope.md"))
        assert gc.main() == EXIT_CALLER_ERROR
        assert "not a readable file" in capsys.readouterr().err

    def test_a_directory_is_a_caller_error(self, tmp_path, cli_argv, capsys):
        cli_argv("generate_changelog.py", "--lint", str(tmp_path))
        assert gc.main() == EXIT_CALLER_ERROR
        capsys.readouterr()

    def test_an_undecodable_file_is_a_caller_error(self, tmp_path, cli_argv, capsys):
        f = tmp_path / "CHANGELOG.md"
        f.write_bytes("## [v1.0.0] — 標題 (2026-01-01)\n".encode("cp950"))
        cli_argv("generate_changelog.py", "--lint", str(f))
        assert gc.main() == EXIT_CALLER_ERROR
        assert "could not read" in capsys.readouterr().err

    def test_a_real_finding_is_still_a_violation_not_a_caller_error(
            self, tmp_path, cli_argv, capsys):
        """必響對照組: without it, "always return 2" would pass the three above."""
        f = tmp_path / "CHANGELOG.md"
        f.write_text("## [v1.0.0] — T\n\n### Fixed\n- a\n", encoding="utf-8")
        cli_argv("generate_changelog.py", "--lint", str(f))
        rc = gc.main()
        assert rc == 1, capsys.readouterr()

    def test_repeated_paths_are_not_counted_twice(self, tmp_path, cli_argv, capsys):
        f = tmp_path / "CHANGELOG.md"
        f.write_text("## [Unreleased]\n\n- content, no subsection\n", encoding="utf-8")
        cli_argv("generate_changelog.py", "--lint", str(f), str(f))
        assert gc.main() == 1
        out = capsys.readouterr().out
        assert "1 changelog format issue(s)" in out, out


class TestLintWiring:
    """⛔ The halves are tested above; this pins that they are CONNECTED.

    A file-format linter with no caller is what #1765 was about, so the
    wiring is the part that must not be silently removable.
    """

    @staticmethod
    def _lint_hooks():
        import yaml
        repo = Path(__file__).resolve().parents[2]
        cfg = yaml.safe_load((repo / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
        hooks = [h for r in cfg["repos"] for h in r.get("hooks", [])]
        return repo, [h for h in hooks
                      if "generate_changelog.py --lint" in h.get("entry", "")]

    def test_the_precommit_hook_runs_lint_on_changelog_files(self):
        """⛔ Every assertion here is on BEHAVIOUR, not on substrings.

        Blind review broke the real wiring three ways while a substring-based
        version of this test stayed green: a `files:` pattern that matches
        nothing but still contains the word CHANGELOG, `stages: [manual]`, and
        an entry that still contains `--lint` but can never fail.
        """
        repo, lint = self._lint_hooks()
        assert lint, "no pre-commit hook runs the changelog file linter"
        for h in lint:
            pattern = h.get("files", "")
            # The pattern must actually select the files, and must not be a
            # catch-all that would merely look selective.
            assert re.search(pattern, "CHANGELOG.md"), pattern
            assert re.search(pattern, "CHANGELOG-archive.md"), pattern
            assert not re.search(pattern, "docs/internal/dev-rules.md"), pattern
            # ⛔ It must receive the files it matched. `pass_filenames: false`
            # would make the `files:` pattern decorative — the hook would fire
            # for CHANGELOG-archive.md and lint CHANGELOG.md instead.
            assert h.get("pass_filenames", True) is True, h
            # A hook moved to the manual stage never runs on a commit.
            stages = h.get("stages")
            assert stages is None or any(
                s in ("pre-commit", "commit") for s in stages), h

    def test_the_configured_entry_actually_fails_on_a_broken_file(self, tmp_path):
        """Run the entry AS CONFIGURED. An entry that still reads `--lint` can
        be inert: appending `--help` makes argparse stop consuming paths and
        exit 0 before the linter is ever called."""
        repo, lint = self._lint_hooks()
        broken = tmp_path / "CHANGELOG.md"
        broken.write_text("## [Unreleased]\n\n- a bullet, but no ### heading\n",
                          encoding="utf-8")
        for h in lint:
            argv = h["entry"].split()
            if Path(argv[0]).stem in ("python3", "python", "py"):
                argv[0] = sys.executable
            r = subprocess.run(argv + [str(broken)], cwd=repo, timeout=120,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            assert r.returncode != 0, (
                f"the hook as configured accepted a broken changelog\n"
                f"entry: {h['entry']}\nstdout: {r.stdout}\nstderr: {r.stderr}")

    def test_lint_actually_reads_the_paths_it_is_given(self, tmp_path, capsys, cli_argv):
        """⛔ The other half of `pass_filenames`. A hook that hands the tool a
        path it then ignores is the same shape as this whole ticket: bound to
        a file it never opens. Two files, only the SECOND one is broken."""
        good = tmp_path / "CHANGELOG.md"
        good.write_text("## [v2.9.0] — T (2026-06-06)\n\n### Fixed\n- a\n",
                        encoding="utf-8")
        bad = tmp_path / "CHANGELOG-archive.md"
        bad.write_text("## [v1.0.0] — T (2026-03-01)\n\nno subsections here\n",
                       encoding="utf-8")
        cli_argv("generate_changelog.py", "--lint", str(good), str(bad))
        rc = gc.main()
        out = capsys.readouterr().out
        assert rc != 0, out
        assert "CHANGELOG-archive.md" in out, out

    def test_validate_all_registers_it_and_maps_changelog_to_it(self):
        repo = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(repo / "scripts" / "tools"))
        import validate_all as va
        row = [t for t in va.TOOLS if t[0] == "changelog_format"]
        assert row, "changelog_format is not registered"
        _, script, args, _ = row[0]
        assert script == "dx/generate_changelog.py"
        # ⛔ Paths are explicit. A bare `--lint` defaults to CHANGELOG.md, so
        # this row would lint that file even when selected because
        # CHANGELOG-archive.md changed.
        assert args[0] == "--lint"
        # ⛔ NOT an exact set: that would contradict the invariant below the
        # moment it fires — adding `CHANGELOG.en.md` to the targets is exactly
        # what the invariant demands, and an `==` assertion would call it a
        # regression. What actually matters is stated directly instead:
        # the default target is there, and no target is a file that does not
        # exist (a phantom target makes every run exit 2).
        assert "CHANGELOG.md" in args[1:], args
        for target in args[1:]:
            assert (repo / target).is_file(), (
                f"{target} is a lint target but does not exist; the check "
                f"would exit 2 on every run")
        assert "changelog_format" in va.WATCH_TRIGGERS["CHANGELOG.md"]
        assert "changelog_format" in va.WATCH_TRIGGERS["CHANGELOG-archive.md"]

        # ⛔ Mapped-but-not-linted is claimed coverage that does not exist:
        # `--smart` would select this check because that file changed, and the
        # check would not look at it. Scoped to files that EXIST, because
        # `CHANGELOG.en.md` is mapped as a future file and naming it as a
        # target today would make every run exit 2. This fires the moment
        # someone adds it — which is exactly when the target list must grow.
        mapped = {f for f, checks in va.WATCH_TRIGGERS.items()
                  if "changelog_format" in checks}
        for f in sorted(mapped):
            if (repo / f).is_file():
                assert f in args[1:], (
                    f"{f} is mapped to changelog_format but the check never "
                    f"lints it; targets are {args[1:]}")

    def test_ci_actually_reaches_it(self):
        """⛔ Registered is not the same as reachable — blind review measured
        that gap on this very change.

        The `Lint` job names each hook it runs, and both automated
        `validate_all.py` callers pass an explicit `--only` allow-list. A hook
        or a registry row absent from those lists never executes in CI, exits
        0, and reports nothing: the defect class this ticket is about.
        """
        repo = Path(__file__).resolve().parents[2]
        ci = (repo / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "pre-commit run changelog-format --all-files" in ci

        for rel in (("Makefile",), (".github", "workflows", "docs-ci.yaml")):
            text = repo.joinpath(*rel).read_text(encoding="utf-8")
            only = [ln for ln in text.splitlines() if "--only " in ln]
            assert only, f"no --only line in {rel}"
            assert any("changelog_format" in ln for ln in only), (
                f"{rel} runs validate_all with an --only list that omits "
                f"changelog_format, so the check never runs there")


class TestMain:
    def test_lint_mode_clean_returns_zero(self, monkeypatch, tmp_path, capsys, cli_argv):
        cl = tmp_path / "CHANGELOG.md"
        cl.write_text(
            "## [v2.8.0] — T (2026-05-07)\n\n### X\n- a\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        cli_argv('generate_changelog.py', '--lint')
        assert gc.main() == 0
        out = capsys.readouterr().out
        assert "clean" in out.lower()

    def test_lint_mode_with_issues_returns_one(self, monkeypatch, tmp_path, capsys, cli_argv):
        cl = tmp_path / "CHANGELOG.md"
        # No subsection → lint will flag.
        cl.write_text("## [v2.8.0] — T (2026-05-07)\n\nplain text\n",
                      encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        cli_argv('generate_changelog.py', '--lint')
        assert gc.main() == 1
        out = capsys.readouterr().out
        assert "format issue" in out

    def test_check_mode_all_conventional_returns_zero(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gc, "get_latest_tag", lambda: None)
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [
            ("abc1", "feat: a"),
            ("abc2", "fix(ci): b"),
        ])
        cli_argv('generate_changelog.py', '--check')
        assert gc.main() == 0
        err = capsys.readouterr().err
        assert "follow conventional" in err

    def test_check_mode_with_non_conventional_returns_one(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gc, "get_latest_tag", lambda: "v2.7.0")
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [
            ("abc1", "feat: ok"),
            ("ffff", "broken commit subject"),
        ])
        monkeypatch.setattr(gc, "load_ignored_commits", lambda: [])
        cli_argv('generate_changelog.py', '--check')
        assert gc.main() == 1
        err = capsys.readouterr().err
        assert "non-conventional" in err
        assert "broken commit subject" in err

    def test_check_mode_ignored_commit_skipped(self, monkeypatch, capsys, cli_argv):
        # Non-conventional commit but its SHA prefix is in ignore list.
        monkeypatch.setattr(gc, "get_latest_tag", lambda: None)
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [
            ("abc123def456", "broken legacy subject"),
        ])
        monkeypatch.setattr(gc, "load_ignored_commits", lambda: ["abc123def456"])
        cli_argv('generate_changelog.py', '--check')
        assert gc.main() == 0
        err = capsys.readouterr().err
        assert "skipped via .changelog-lint-ignore" in err

    def test_no_commits_returns_zero(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gc, "get_latest_tag", lambda: "v2.7.0")
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [])
        cli_argv('generate_changelog.py')
        assert gc.main() == 0
        err = capsys.readouterr().err
        assert "No commits" in err

    def test_default_run_prints_to_stdout(self, monkeypatch, capsys, cli_argv):
        monkeypatch.setattr(gc, "get_latest_tag", lambda: "v2.7.0")
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [
            ("abc1", "feat(api): add endpoint"),
        ])
        cli_argv('generate_changelog.py')
        assert gc.main() == 0
        out = capsys.readouterr().out
        assert "## [UNRELEASED]" in out
        assert "add endpoint" in out
        # Stats footer present.
        assert "<!-- Stats:" in out

    def test_output_flag_writes_file(self, monkeypatch, tmp_path, cli_argv):
        out_path = tmp_path / "draft.md"
        monkeypatch.setattr(gc, "get_latest_tag", lambda: None)
        monkeypatch.setattr(gc, "get_commits_since", lambda since: [
            ("abc1", "feat: x"),
        ])
        cli_argv("generate_changelog.py", "-o", str(out_path))
        assert gc.main() == 0
        assert out_path.exists()
        content = out_path.read_text(encoding="utf-8")
        assert "## [UNRELEASED]" in content

    def test_since_flag_overrides_latest_tag(self, monkeypatch, capsys, cli_argv):
        captured = {}

        def fake_get_commits(since_ref):
            captured["since"] = since_ref
            return []

        monkeypatch.setattr(gc, "get_commits_since", fake_get_commits)
        # get_latest_tag should NOT be called when --since is provided.
        monkeypatch.setattr(gc, "get_latest_tag",
                            lambda: pytest.fail("get_latest_tag should be skipped"))
        cli_argv('generate_changelog.py', '--since', 'v2.5.0')
        assert gc.main() == 0
        assert captured["since"] == "v2.5.0"


# ── Entry cap: new [Unreleased] entries only (agent 指引改善計畫 PR-C) ──


def _entry(head: str, chars: int) -> str:
    """A top-level entry padded to exactly ``chars`` characters."""
    body = f"- **{head}**: "
    return body + "x" * (chars - len(body))


def _unreleased(*entries: str) -> str:
    return "## [Unreleased]\n\n### Changed\n" + "\n".join(entries) + "\n"


class TestNewEntryCap:
    """Cap on NEW entries; existing ones are never retroactive (the file has
    hundreds over the cap and #1737 decided not to rewrite history)."""

    def test_new_oversize_entry_is_flagged_and_the_split_is_clean(self):
        base = _unreleased(_entry("old", 200))
        big = _unreleased(_entry("old", 200), _entry("new", 1500))
        issues = gc.lint_entry_caps(big, base)
        assert len(issues) == 1 and "1500 chars (> 1000)" in issues[0], issues
        split = _unreleased(_entry("old", 200), _entry("new-a", 700), _entry("new-b", 700))
        assert gc.lint_entry_caps(split, base) == []

    def test_boundary_exactly_cap_is_clean_and_one_over_is_not(self):
        base = _unreleased(_entry("old", 200))
        assert gc.lint_entry_caps(_unreleased(_entry("old", 200), _entry("n", 1000)), base) == []
        assert len(gc.lint_entry_caps(_unreleased(_entry("old", 200), _entry("n", 1001)), base)) == 1

    def test_existing_oversize_entry_is_not_retroactive(self):
        text = _unreleased(_entry("legacy", 3000))
        assert gc.lint_entry_caps(text, text) == []

    def test_editing_a_legacy_entry_below_its_bullet_line_keeps_it_exempt(self):
        legacy = _entry("legacy", 3000)
        base = _unreleased(legacy)
        edited = _unreleased(legacy + "\n  - a new sub-bullet added later")
        assert gc.lint_entry_caps(edited, base) == []

    def test_copying_a_base_bullet_line_does_not_buy_a_second_exempt_entry(self):
        """Blind review: keys were a set, so pasting an existing bullet line
        above 1200 new chars made the whole entry 'existing'."""
        base = _unreleased(_entry("old", 200))
        text = _unreleased(_entry("old", 200), _entry("old", 200) + "\n  " + "x" * 1200)
        issues = gc.lint_entry_caps(text, base)
        assert len(issues) == 1 and "chars (> 1000)" in issues[0], issues

    def test_rewording_a_legacy_bullet_line_keeps_the_entry_exempt(self):
        """Fixing a typo in the bullet line of an 18,905-char legacy entry must
        not demand that the history be rewritten: the tail identifies it."""
        legacy = _entry("legacy", 100) + "\n  - " + "x" * 2900   # bullet line + a tail
        base = _unreleased(legacy)
        reworded = legacy.replace("- **legacy**:", "- **legacy (typo fixed)**:", 1)
        assert gc.lint_entry_caps(_unreleased(reworded), base) == []
        # A one-line entry HAS no tail: rewording its bullet line rewrites the
        # whole entry, and that is new content.
        one_liner = _entry("solo", 1500)
        assert len(gc.lint_entry_caps(_unreleased(one_liner.replace("solo", "solo2")), _unreleased(one_liner))) == 1

    def test_a_base_that_is_not_the_parent_collapses_to_one_finding(self):
        """Release wrap-up moved [Unreleased] out of the base: 400 'new
        entries' are one fact, not 400 findings."""
        entries = [_entry(f"e{i}", 1500) for i in range(30)]
        text = _unreleased(*entries)
        issues = gc.lint_entry_caps(text, _unreleased(_entry("other", 50)), base_label="origin/main")
        assert len(issues) == 1 and "30 new entries" in issues[0] and "origin/main" in issues[0], issues

    def test_no_base_means_everything_is_new(self):
        text = _unreleased(_entry("a", 1500))
        assert len(gc.lint_entry_caps(text, None)) == 1

    def test_sub_bullets_and_evidence_count_toward_the_length(self):
        base = _unreleased(_entry("old", 200))
        short_head = "- **n**: x"
        padding = "\n  - " + "y" * 1200
        text = _unreleased(_entry("old", 200), short_head + padding)
        assert len(gc.lint_entry_caps(text, base)) == 1

    def test_other_sections_are_not_capped(self):
        text = "## [v2.9.0] — T (2026-06-06)\n\n### Fixed\n" + _entry("released", 5000) + "\n"
        assert gc.lint_entry_caps(text, "") == []

    def test_cap_zero_disables_via_cli(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "history.md"
        f.write_text(_unreleased(_entry("n", 1500)), encoding="utf-8")
        monkeypatch.setattr(gc, "_git_show", lambda ref, path: _unreleased(_entry("old", 5)))
        monkeypatch.setattr(sys, "argv", ["gc", "--cap", "0", "--lint", str(f)])
        assert gc.main() == 0
        monkeypatch.setattr(sys, "argv", ["gc", "--lint", str(f)])
        assert gc.main() == 1
        out = capsys.readouterr().out
        assert "1500 chars (> 1000)" in out


class TestColumnZeroTextIsAnError:
    """A column-0 line closes the entry above it (that is how
    `agent_output_metrics.iter_entries` reads the file), so it is the one way
    to split an entry around the cap. New ones are errors; legacy ones stay."""

    def test_new_column0_text_is_flagged_with_its_line_number(self):
        base = _unreleased(_entry("old", 200))
        text = "## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n⇒ out-dented conclusion\n"
        issues = gc.lint_entry_caps(text, base)
        assert len(issues) == 1 and issues[0].startswith("L5:") and "belongs to no entry" in issues[0], issues

    def test_legacy_column0_lines_are_keyed_against_the_base(self):
        text = "## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n| a | b |\n|---|---|\n"
        assert gc.lint_entry_caps(text, text) == []

    @pytest.mark.parametrize("line", ["### Fixed", "<!-- note -->", "- another bullet", "  continuation", "\tcontinuation"])
    def test_structural_and_continuation_lines_belong_somewhere(self, line):
        """A heading/comment is structure; an indented line directly under an
        open bullet is that bullet's continuation. None of these is a gap."""
        base = _unreleased(_entry("old", 200))
        text = "## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n" + line + "\n"
        assert [i for i in gc.lint_entry_caps(text, base) if "belongs to no entry" in i] == []

    def test_indented_bullet_before_any_entry_is_open_is_a_gap(self):
        """Blind-review BLOCK: `  - **fat**` as the first thing under a
        heading renders as a normal list item but `iter_entries` drops it,
        so 1200 chars were invisible to the cap. Same for a tab."""
        base = _unreleased(_entry("old", 200))
        for indent in ("  ", "\t"):
            text = "## [Unreleased]\n\n### Changed\n" + indent + _entry("fat", 1200) + "\n" + _entry("old", 200) + "\n"
            issues = gc.lint_entry_caps(text, base)
            assert any("belongs to no entry" in i for i in issues), (indent, issues)

    @pytest.mark.parametrize("splitter", ["<!-- -->", "### 續"])
    def test_text_split_off_by_a_comment_or_heading_is_a_gap(self, splitter):
        """The splitter itself is structure, but the indented text after it
        belongs to no entry — that text is what the split was hiding."""
        base = _unreleased(_entry("old", 200))
        text = ("## [Unreleased]\n\n### Changed\n" + _entry("n", 900) + "\n" + splitter + "\n"
                + "  " + "y" * 900 + "\n  " + "y" * 900 + "\n")
        issues = gc.lint_entry_caps(text, base)
        assert sum("belongs to no entry" in i for i in issues) == 2, issues

    def test_a_legacy_column0_line_reused_as_a_splitter_still_exposes_the_tail(self):
        base = "## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n|---|---|\n"
        text = ("## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n|---|---|\n"
                + _entry("n", 900) + "\n|---|---|\n  " + "y" * 900 + "\n")
        issues = gc.lint_entry_caps(text, base)
        assert sum("belongs to no entry" in i for i in issues) == 1, issues

    def test_splitting_an_oversize_entry_with_column0_text_does_not_evade(self):
        """One shape of the evasion this check exists for: 600 + out-dent +
        600 would be two entries of 600 without it (the other shapes —
        indented bullet, comment, heading, legacy splitter — are above)."""
        base = _unreleased(_entry("old", 200))
        text = "## [Unreleased]\n\n### Changed\n" + _entry("old", 200) + "\n" + _entry("n", 600) + "\ntail " + "z" * 600 + "\n"
        issues = gc.lint_entry_caps(text, base)
        assert any("belongs to no entry" in i for i in issues), issues


class TestCapBase:
    def test_default_base_is_head_without_github_base_ref(self, monkeypatch):
        monkeypatch.setattr(gc, "_ref_exists", lambda ref: True)
        assert gc.default_cap_base({}) == "HEAD"

    def test_pr_run_uses_origin_base_when_fetched(self, monkeypatch):
        monkeypatch.setattr(gc, "_ref_exists", lambda ref: ref == "origin/main")
        assert gc.default_cap_base({"GITHUB_BASE_REF": "main"}) == "origin/main"

    def test_pr_run_without_the_fetched_base_is_none_not_head(self, monkeypatch):
        """Falling back to HEAD on a PR run would be a cap that silently does
        not exist; the answer is None and main() turns it into exit 2."""
        monkeypatch.setattr(gc, "_ref_exists", lambda ref: False)
        assert gc.default_cap_base({"GITHUB_BASE_REF": "main"}) is None

    def test_unfetched_pr_base_is_a_caller_error_naming_the_fetch(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "history.md"
        f.write_text(_unreleased(_entry("n", 10)), encoding="utf-8")
        monkeypatch.setenv("GITHUB_BASE_REF", "main")
        monkeypatch.setattr(gc, "_ref_exists", lambda ref: False)
        monkeypatch.setattr(sys, "argv", ["gc", "--lint", str(f)])
        assert gc.main() == EXIT_CALLER_ERROR
        assert "git fetch --no-tags origin main" in capsys.readouterr().err

    def test_negative_cap_is_a_caller_error_not_a_silent_off_switch(self, tmp_path, monkeypatch):
        f = tmp_path / "history.md"
        f.write_text(_unreleased(_entry("n", 1500)), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["gc", "--cap", "-1", "--lint", str(f)])
        with pytest.raises(SystemExit) as exc:
            gc.main()
        assert exc.value.code == 2

    def test_missing_base_file_prints_a_notice_and_counts_everything(self, tmp_path, monkeypatch, capsys):
        f = tmp_path / "history.md"
        f.write_text(_unreleased(_entry("n", 1500)), encoding="utf-8")
        monkeypatch.setattr(gc, "_git_show", lambda ref, path: None)
        monkeypatch.setattr(sys, "argv", ["gc", "--base", "HEAD", "--lint", str(f)])
        assert gc.main() == 1
        captured = capsys.readouterr()
        assert "counts as new" in captured.out and "1500 chars" in captured.out

    def test_git_show_reads_the_committed_version(self, tmp_path, monkeypatch):
        """Real git, no mocks: the working tree may differ from HEAD."""
        repo = tmp_path / "r"
        repo.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, timeout=60)
        run("init", "-q")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        f = repo / "history.md"
        f.write_text("committed\n", encoding="utf-8")
        run("add", "history.md")
        run("commit", "-q", "-m", "c")
        f.write_text("working tree\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        assert gc._git_show("HEAD", "history.md") == "committed\n"
        assert gc._git_show("HEAD", "MISSING.md") is None
        assert gc._git_show("nosuchref", "history.md") is None
