"""Tests for check_repo_name.py — Prevent wrong repository name in source files."""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_repo_name as crn  # noqa: E402


# ---------------------------------------------------------------------------
# scan_file
# ---------------------------------------------------------------------------
class TestScanFile:
    def test_no_violations(self, tmp_path):
        f = tmp_path / "clean.md"
        f.write_text("See https://github.com/vencil/Dynamic-Alerting-Integrations\n",
                      encoding="utf-8")
        violations = crn.scan_file(str(f))
        assert violations == []

    def test_detects_wrong_name(self, tmp_path):
        f = tmp_path / "bad.md"
        f.write_text("Clone https://github.com/vencil/vibe-k8s-lab\n",
                      encoding="utf-8")
        violations = crn.scan_file(str(f))
        assert len(violations) == 1
        assert violations[0][0] == 1  # line number

    def test_allows_docker_path(self, tmp_path):
        f = tmp_path / "docker.md"
        f.write_text("docker exec -w /workspaces/vibe-k8s-lab container cmd\n",
                      encoding="utf-8")
        violations = crn.scan_file(str(f))
        assert violations == []

    def test_allows_cluster_name(self, tmp_path):
        f = tmp_path / "kind.md"
        f.write_text("kind: vibe-k8s-lab-cluster\n", encoding="utf-8")
        violations = crn.scan_file(str(f))
        assert violations == []

    def test_fix_mode(self, tmp_path):
        f = tmp_path / "fixme.md"
        f.write_text("See https://github.com/vencil/vibe-k8s-lab/tree/main\n",
                      encoding="utf-8")
        violations = crn.scan_file(str(f), fix=True)
        assert len(violations) == 1
        content = f.read_text(encoding="utf-8")
        assert "Dynamic-Alerting-Integrations" in content
        assert "vibe-k8s-lab" not in content

    def test_multiple_violations(self, tmp_path):
        f = tmp_path / "multi.md"
        f.write_text(
            "Line 1: https://github.com/vencil/vibe-k8s-lab\n"
            "Line 2: clean\n"
            "Line 3: https://github.com/vencil/vibe-k8s-lab/issues\n",
            encoding="utf-8",
        )
        violations = crn.scan_file(str(f))
        assert len(violations) == 2
        assert violations[0][0] == 1
        assert violations[1][0] == 3

    def test_unicode_decode_error(self, tmp_path):
        f = tmp_path / "binary.bin"
        f.write_bytes(b"\x80\x81\x82\x83")
        violations = crn.scan_file(str(f))
        assert violations == []

    def test_no_fix_when_clean(self, tmp_path):
        f = tmp_path / "clean.md"
        content = "Everything is fine\n"
        f.write_text(content, encoding="utf-8")
        violations = crn.scan_file(str(f), fix=True)
        assert violations == []
        assert f.read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    def test_correct_repo_name(self):
        assert crn.CORRECT_REPO == "Dynamic-Alerting-Integrations"

    def test_wrong_pattern_matches(self):
        assert crn.WRONG_PATTERN.search("github.com/vencil/vibe-k8s-lab")

    def test_wrong_pattern_no_match(self):
        assert not crn.WRONG_PATTERN.search("github.com/vencil/Dynamic-Alerting-Integrations")

    def test_scan_extensions(self):
        assert ".md" in crn.SCAN_EXTENSIONS
        assert ".py" in crn.SCAN_EXTENSIONS
        assert ".yaml" in crn.SCAN_EXTENSIONS


# ---------------------------------------------------------------------------
# main (CLI)
# ---------------------------------------------------------------------------
class TestMain:
    """v2.8.0 lint-policy refactor (PR #382/#383): tests use --full-scan to
    exercise the os.walk-based path. Diff-only mode requires git rev-parse
    in a real repo, which TestMainDiffMode covers separately."""

    def test_ci_mode_no_violations(self, capsys):
        with patch.object(crn, "REPO_ROOT", crn.Path("/nonexistent")):
            with patch("os.walk", return_value=[]):
                with patch("sys.argv",
                           ["check_repo_name.py", "--full-scan", "--ci"]):
                    rc = crn.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "no wrong repo name found" in captured.out.lower()

    def test_ci_mode_with_violations(self, tmp_path, capsys):
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv",
                       ["check_repo_name.py", "--full-scan", "--ci"]):
                rc = crn.main()
        assert rc == 1

    def test_fix_mode_reports(self, tmp_path, capsys):
        bad_file = tmp_path / "fixme.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv",
                       ["check_repo_name.py", "--full-scan", "--fix"]):
                rc = crn.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "Fixed" in captured.out

    def test_fix_mode_requires_full_scan(self, tmp_path, capsys):
        """--fix without --full-scan must error (partial-line rewrites unsafe)."""
        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", ["check_repo_name.py", "--fix"]):
                rc = crn.main()
        assert rc == 2  # error exit code

    def test_skips_excluded_dirs(self, tmp_path, capsys):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        bad_file = git_dir / "config.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv",
                       ["check_repo_name.py", "--full-scan"]):
                rc = crn.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "no wrong repo name found" in captured.out.lower()


class TestBypass:
    """Bypass tag mechanism per lint-policy.md §4."""

    def test_bypass_tag_in_pr_body_downgrades_to_warning(
        self, tmp_path, capsys, monkeypatch
    ):
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        pr_body = (
            "## Summary\n\n"
            "bypass-lint: repo-name\n"
            "reason: This PR intentionally references the historical repo name "
            "for migration documentation purposes.\n"
        )
        monkeypatch.setenv("PR_BODY", pr_body)

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv",
                       ["check_repo_name.py", "--full-scan", "--ci"]):
                rc = crn.main()
        assert rc == 0  # bypass turns hard-fail into exit 0
        captured = capsys.readouterr()
        assert "BYPASSED" in captured.out

    def test_bypass_for_other_lint_does_not_match(
        self, tmp_path, capsys, monkeypatch
    ):
        """Bypass tag for a different lint name must NOT trigger."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")
        monkeypatch.setenv(
            "PR_BODY",
            "bypass-lint: codename-leak\nreason: For a different lint entirely.\n",
        )
        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv",
                       ["check_repo_name.py", "--full-scan", "--ci"]):
                rc = crn.main()
        assert rc == 1  # No matching bypass → still fails


class TestLineViolates:
    """Pure helper covers the negative-pattern + allowlist intersection."""

    def test_clean_line_does_not_violate(self):
        assert not crn._line_violates("github.com/vencil/Dynamic-Alerting-Integrations")

    def test_dirty_line_violates(self):
        assert crn._line_violates("https://github.com/vencil/vibe-k8s-lab/issues")

    def test_workspace_path_excused(self):
        assert not crn._line_violates(
            "WORKDIR /workspaces/vibe-k8s-lab "
            "and github.com/vencil/vibe-k8s-lab"
        )

    def test_kind_cluster_name_excused(self):
        assert not crn._line_violates("kind cluster: vibe-k8s-lab-cluster")


class TestDiffAware:
    """Diff-only mode tests (mock get_diff_added_lines to avoid real git)."""

    def test_scan_file_diff_only_flags_added_lines(self, tmp_path):
        """scan_file_diff should only return violations on lines from
        get_diff_added_lines, not full file scan."""
        f = tmp_path / "mixed.md"
        f.write_text(
            "Line 1: clean text\n"
            "Line 2: github.com/vencil/vibe-k8s-lab (preexisting)\n"
            "Line 3: github.com/vencil/vibe-k8s-lab (newly added)\n",
            encoding="utf-8",
        )
        # Only line 3 is in the diff
        with patch.object(
            crn, "get_diff_added_lines",
            return_value=[(3, "Line 3: github.com/vencil/vibe-k8s-lab (newly added)")],
        ):
            violations = crn.scan_file_diff(str(f), base="origin/main")
        assert len(violations) == 1
        assert violations[0][0] == 3  # line number in current file

    def test_scan_file_diff_falls_back_on_git_error(self, tmp_path):
        """If git diff subprocess fails, fall back to full-file scan."""
        import subprocess
        f = tmp_path / "any.md"
        f.write_text("github.com/vencil/vibe-k8s-lab\n", encoding="utf-8")

        def raise_called_process_error(*a, **kw):
            raise subprocess.CalledProcessError(1, "git")

        with patch.object(
            crn, "get_diff_added_lines",
            side_effect=raise_called_process_error,
        ):
            violations = crn.scan_file_diff(str(f), base="origin/main")
        # fallback finds the violation
        assert len(violations) == 1

    def test_iter_scan_targets_diff_mode_uses_git_diff(self, tmp_path):
        """Diff-only iter should call git diff --name-only and yield matching
        files only."""
        # Create a few files in tmp
        (tmp_path / "a.md").write_text("test", encoding="utf-8")
        (tmp_path / "b.md").write_text("test", encoding="utf-8")
        (tmp_path / "c.txt").write_text("test", encoding="utf-8")  # not in scan exts

        import subprocess
        mock_result = subprocess.CompletedProcess(
            args=["git", "diff"], returncode=0,
            stdout="a.md\nc.txt\n",  # c.txt should be filtered out by extension
        )
        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch.object(crn.subprocess, "run", return_value=mock_result):
                targets = list(crn.iter_scan_targets(
                    full_scan=False, base="origin/main",
                ))
        assert len(targets) == 1
        assert targets[0][1] == "a.md"

    def test_diff_base_missing_returns_exit_2(self, capsys, monkeypatch):
        """When resolve_diff_base raises DiffBaseMissingError, main exits 2."""
        from _lint_helpers import DiffBaseMissingError
        monkeypatch.setenv("LINT_DIFF_BASE", "origin/nonexistent-branch")
        with patch.object(crn, "REPO_ROOT", crn.Path("/tmp")):
            # NOTE: actual git rev-parse will fail, no need to mock
            with patch("sys.argv", ["check_repo_name.py", "--ci"]):
                rc = crn.main()
        assert rc == 2

    def test_pr_body_file_overrides_env(self, tmp_path, capsys, monkeypatch):
        """--pr-body-file takes priority over $PR_BODY env var for bypass."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        body_file = tmp_path / "pr_body.txt"
        body_file.write_text(
            "bypass-lint: repo-name\n"
            "reason: From file rather than env, for testing precedence here.\n",
            encoding="utf-8",
        )
        # Env says no bypass
        monkeypatch.setenv("PR_BODY", "no bypass here at all")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", [
                "check_repo_name.py", "--full-scan", "--ci",
                "--pr-body-file", str(body_file),
            ]):
                rc = crn.main()
        assert rc == 0  # File bypass wins
        captured = capsys.readouterr()
        assert "BYPASSED" in captured.out

    def test_pr_body_file_missing_falls_back_to_env(self, tmp_path, capsys, monkeypatch):
        """--pr-body-file pointing to missing file falls back to $PR_BODY."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")
        monkeypatch.setenv(
            "PR_BODY",
            "bypass-lint: repo-name\nreason: env fallback works correctly here.\n",
        )

        missing = tmp_path / "does-not-exist.txt"
        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", [
                "check_repo_name.py", "--full-scan", "--ci",
                "--pr-body-file", str(missing),
            ]):
                rc = crn.main()
        assert rc == 0  # env bypass kicks in
        captured = capsys.readouterr()
        assert "BYPASSED" in captured.out
