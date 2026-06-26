"""Tests for check_orphan_lint.py — orphan / dead-lint detector (#717)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_orphan_lint as ol  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# find_orphans — the core decision
# ---------------------------------------------------------------------------
class TestFindOrphans:
    """A check is orphan iff its filename is absent from the runner corpus."""

    def test_wired_check_passes(self):
        corpus = "entry: python3 scripts/tools/lint/check_foo.py --ci\n"
        assert ol.find_orphans(["check_foo.py"], corpus) == []

    def test_orphan_check_flagged(self):
        corpus = "entry: python3 scripts/tools/lint/check_other.py --ci\n"
        assert ol.find_orphans(["check_foo.py"], corpus) == ["check_foo.py"]

    def test_dogfood_unregistered_stub_is_caught(self):
        """AC dogfood: an unregistered check_xxx_stub.py must be flagged."""
        corpus = "nothing references the stub here"
        orphans = ol.find_orphans(
            ["check_xxx_stub.py", "check_wired.py"],
            corpus + " check_wired.py ")
        assert orphans == ["check_xxx_stub.py"]

    def test_allowlist_suppresses_orphan(self):
        corpus = "no references at all"
        allow = {"check_manual_only.py": "manual forensic tool (#NNN)"}
        assert ol.find_orphans(["check_manual_only.py"], corpus, allow) == []

    def test_allowlist_does_not_hide_other_orphans(self):
        corpus = ""
        allow = {"check_manual_only.py": "ok"}
        orphans = ol.find_orphans(
            ["check_manual_only.py", "check_real_orphan.py"], corpus, allow)
        assert orphans == ["check_real_orphan.py"]

    def test_result_is_sorted_subset_of_input(self):
        corpus = ""
        out = ol.find_orphans(["check_b.py", "check_a.py"], corpus)
        assert out == ["check_b.py", "check_a.py"]  # preserves input order


# ---------------------------------------------------------------------------
# find_check_lints — glob scope
# ---------------------------------------------------------------------------
class TestFindCheckLints:
    """Only check_*.py are candidates; helpers (_-prefixed) are excluded."""

    def test_globs_only_check_prefix(self, tmp_path):
        (tmp_path / "check_alpha.py").write_text("", encoding="utf-8")
        (tmp_path / "check_beta.py").write_text("", encoding="utf-8")
        (tmp_path / "_lint_helpers.py").write_text("", encoding="utf-8")
        (tmp_path / "_version_patterns.py").write_text("", encoding="utf-8")
        (tmp_path / "lint_jsx_babel.py").write_text("", encoding="utf-8")
        (tmp_path / "check_gamma.sh").write_text("", encoding="utf-8")
        assert ol.find_check_lints(tmp_path) == [
            "check_alpha.py", "check_beta.py"]


# ---------------------------------------------------------------------------
# gather_referencers — runner corpus boundary
# ---------------------------------------------------------------------------
class TestGatherReferencers:
    """The lint/ dir must NOT count as a referencer (prose cross-refs)."""

    def _scaffold(self, root: Path) -> Path:
        lint_dir = root / "scripts" / "tools" / "lint"
        lint_dir.mkdir(parents=True)
        (root / "scripts" / "tools" / "validate_all.py").write_text(
            "TOOLS = []\n", encoding="utf-8")
        (root / ".pre-commit-config.yaml").write_text("repos: []\n",
                                                      encoding="utf-8")
        (root / "Makefile").write_text("all:\n", encoding="utf-8")
        wf = root / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: ci\n", encoding="utf-8")
        return lint_dir

    def test_lint_dir_excluded_from_referencers(self, tmp_path):
        lint_dir = self._scaffold(tmp_path)
        # A lint file that mentions another check in prose — must NOT be a referencer.
        (lint_dir / "check_a.py").write_text(
            '"""see check_b.py for details"""\n', encoding="utf-8")
        (lint_dir / "check_b.py").write_text("", encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        assert all(p.parent != lint_dir for p in refs)

    def test_dx_sibling_counts_as_referencer(self, tmp_path):
        lint_dir = self._scaffold(tmp_path)
        dx = tmp_path / "scripts" / "tools" / "dx"
        dx.mkdir(parents=True)
        (dx / "pr_preflight.py").write_text(
            "run('check_a.py')\n", encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        assert (dx / "pr_preflight.py") in refs

    def test_prose_mention_in_lint_does_not_rescue_orphan(self, tmp_path):
        """End-to-end: a check referenced ONLY by another lint's prose is orphan."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        (lint_dir / "check_b.py").write_text(
            '"""invokes check_a.py conceptually"""\n', encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        corpus = ol.read_corpus(refs)
        checks = ol.find_check_lints(lint_dir)
        assert "check_a.py" in ol.find_orphans(checks, corpus)

    def test_validate_all_reference_rescues(self, tmp_path):
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        (tmp_path / "scripts" / "tools" / "validate_all.py").write_text(
            'TOOLS = [("a", "lint/check_a.py", [], "x")]\n', encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        corpus = ol.read_corpus(refs)
        assert ol.find_orphans(["check_a.py"], corpus) == []

    def test_gitlab_ci_reference_rescues(self, tmp_path):
        """A lint wired only into GitLab CI must not be false-flagged."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        gl = tmp_path / ".gitlab" / "ci"
        gl.mkdir(parents=True)
        (gl / "lint.gitlab-ci.yml").write_text(
            "lint:\n  script: python3 scripts/tools/lint/check_a.py --ci\n",
            encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        assert ol.find_orphans(["check_a.py"], ol.read_corpus(refs)) == []


# ---------------------------------------------------------------------------
# Real-repo integration — the wired repo must be clean
# ---------------------------------------------------------------------------
class TestRealRepo:
    """Against the actual tree, every lint is wired → exit 0."""

    def test_repo_is_clean(self):
        script = _REPO_ROOT / "scripts" / "tools" / "lint" / "check_orphan_lint.py"
        result = subprocess.run(
            [sys.executable, str(script), "--ci"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, cwd=str(_REPO_ROOT),
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_dogfood_planted_stub_in_scaffold(self, tmp_path):
        """AC dogfood, xdist-safe: plant an unwired check_xxx_stub.py into a
        scaffolded tmp repo (NOT the shared real lint dir — that would race a
        concurrent test_repo_is_clean under pytest -n auto) and assert the full
        gather_referencers→find_orphans path flags it, while a wired sibling
        passes."""
        lint_dir = tmp_path / "scripts" / "tools" / "lint"
        lint_dir.mkdir(parents=True)
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "entry: scripts/tools/lint/check_wired.py --ci\n", encoding="utf-8")
        (tmp_path / "scripts" / "tools" / "validate_all.py").write_text(
            "TOOLS = []\n", encoding="utf-8")
        (lint_dir / "check_wired.py").write_text("", encoding="utf-8")
        (lint_dir / "check_xxx_stub.py").write_text(
            "# unwired stub\n", encoding="utf-8")

        refs = ol.gather_referencers(tmp_path, lint_dir)
        corpus = ol.read_corpus(refs)
        orphans = ol.find_orphans(ol.find_check_lints(lint_dir), corpus)
        assert orphans == ["check_xxx_stub.py"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
