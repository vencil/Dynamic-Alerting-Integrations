"""Extension tests for generate_changelog.py — git/IO + lint + main coverage.

Audit flagged 28.5% coverage. The existing test_generate_changelog.py
covers parse_commit / format_changelog / regex / constants — the pure
logic. This file fills the orchestrator gap:
  - git_cmd (subprocess wrapper, success + error)
  - get_latest_tag (git describe with optional fallback)
  - get_commits_since (git log + parse)
  - load_ignored_commits (file-based ignore list)
  - lint_changelog (full markdown lint)
  - main() (default / --check / --lint / --output / --since)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import generate_changelog as gc  # noqa: E402


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


# ---------------------------------------------------------------------------
# git_cmd
# ---------------------------------------------------------------------------
class TestGitCmd:
    def test_success_returns_stripped_stdout(self, monkeypatch):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(0, "  hello\n", ""))
        assert gc.git_cmd(["log"]) == "hello"

    def test_failure_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(gc.subprocess, "run",
                            lambda *a, **kw: _cp(128, "", "fatal: not a repo"))
        with pytest.raises(SystemExit) as exc:
            gc.git_cmd(["log"])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "git log failed" in err
        assert "not a repo" in err


# ---------------------------------------------------------------------------
# get_latest_tag
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# get_commits_since
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# load_ignored_commits
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# lint_changelog
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# main — CLI orchestrator
# ---------------------------------------------------------------------------
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
