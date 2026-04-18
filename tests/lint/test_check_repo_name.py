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
    def test_ci_mode_no_violations(self, capsys):
        with patch.object(crn, "REPO_ROOT", crn.Path("/nonexistent")):
            with patch("os.walk", return_value=[]):
                with patch("sys.argv", ["check_repo_name.py", "--ci"]):
                    crn.main()
        captured = capsys.readouterr()
        assert "No wrong repo name found" in captured.out

    def test_ci_mode_with_violations(self, tmp_path, capsys):
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", ["check_repo_name.py", "--ci"]):
                with pytest.raises(SystemExit) as exc_info:
                    crn.main()
        assert exc_info.value.code == 1

    def test_fix_mode_reports(self, tmp_path, capsys):
        bad_file = tmp_path / "fixme.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", ["check_repo_name.py", "--fix"]):
                crn.main()
        captured = capsys.readouterr()
        assert "Fixed" in captured.out

    def test_skips_excluded_dirs(self, tmp_path, capsys):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        bad_file = git_dir / "config.md"
        bad_file.write_text("https://github.com/vencil/vibe-k8s-lab\n",
                            encoding="utf-8")

        with patch.object(crn, "REPO_ROOT", tmp_path):
            with patch("sys.argv", ["check_repo_name.py"]):
                crn.main()
        captured = capsys.readouterr()
        assert "No wrong repo name found" in captured.out
