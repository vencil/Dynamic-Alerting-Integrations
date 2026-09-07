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

    def test_longer_name_does_not_mask_shorter(self):
        """A reference to disable_check_foo.py must NOT rescue check_foo.py."""
        corpus = "entry: scripts/tools/lint/disable_check_foo.py --ci\n"
        assert ol.find_orphans(["check_foo.py"], corpus) == ["check_foo.py"]

    def test_underscore_prefix_does_not_mask(self):
        corpus = "run old_check_foo.py\n"
        assert ol.find_orphans(["check_foo.py"], corpus) == ["check_foo.py"]

    def test_pyc_bytecode_does_not_mask(self):
        corpus = "stale: check_foo.pyc\n"
        assert ol.find_orphans(["check_foo.py"], corpus) == ["check_foo.py"]

    def test_real_path_reference_still_matches(self):
        """Genuine path/quote/colon-delimited references must still rescue."""
        for ref in ("scripts/tools/lint/check_foo.py --ci",
                    '"check_foo.py"', "check_foo.py:", "- check_foo.py\n"):
            assert ol.find_orphans(["check_foo.py"], ref) == [], ref


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

    # ── #1492: registry membership is not execution ──────────────────

    def _registry(self, tmp_path, keys):
        rows = ", ".join(f'("{k}", "lint/check_{k}.py", [], "x")' for k in keys)
        (tmp_path / "scripts" / "tools" / "validate_all.py").write_text(
            f"TOOLS = [{rows}]\n", encoding="utf-8")

    def _orphans(self, tmp_path, lint_dir):
        refs = ol.gather_referencers(tmp_path, lint_dir)
        corpus = ol.read_corpus(refs)
        reachable = ol.registry_reachable(tmp_path, refs)
        return ol.find_orphans(ol.find_check_lints(lint_dir), corpus, None, reachable)

    def test_registry_membership_alone_does_not_rescue(self, tmp_path):
        """The #1492 defect: a TOOLS row that no caller selects is dead."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a"])
        refs = ol.gather_referencers(tmp_path, lint_dir)
        assert all(p.name != "validate_all.py" for p in refs), refs
        assert self._orphans(tmp_path, lint_dir) == ["check_a.py"]

    def test_only_list_selects_the_row(self, tmp_path):
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        (lint_dir / "check_b.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a", "b"])
        (tmp_path / "Makefile").write_text(
            "lint-docs:\n\t@python3 ./scripts/tools/validate_all.py --only a --ci\n",
            encoding="utf-8")
        assert self._orphans(tmp_path, lint_dir) == ["check_b.py"]

    def test_only_list_across_a_backslash_continuation(self, tmp_path):
        """The real Makefile spreads --only over a continued line."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a"])
        (tmp_path / "Makefile").write_text(
            "lint-docs:\n\t@python3 ./scripts/tools/validate_all.py \\\n"
            "\t\t--only versions,a \\\n\t\t$(ARGS)\n", encoding="utf-8")
        assert self._orphans(tmp_path, lint_dir) == []

    def test_bare_caller_reaches_every_row(self, tmp_path):
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a"])
        wf = tmp_path / ".github" / "workflows" / "ci.yml"
        wf.write_text("run: python scripts/tools/validate_all.py --ci\n", encoding="utf-8")
        assert self._orphans(tmp_path, lint_dir) == []

    def test_prose_and_comment_mentions_are_not_calls(self, tmp_path):
        """A YAML job name, a comment, or a docstring usage line that says
        validate_all.py must not count as a bare (reach-everything) caller."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a"])
        wf = tmp_path / ".github" / "workflows" / "ci.yml"
        wf.write_text("name: Drift Detection (validate_all.py)\n"
                      "# python3 scripts/tools/validate_all.py --ci\n", encoding="utf-8")
        dx = tmp_path / "scripts" / "tools" / "dx"
        dx.mkdir(parents=True)
        (dx / "tool.py").write_text('"""usage: validate_all.py --ci"""\n', encoding="utf-8")
        assert self._orphans(tmp_path, lint_dir) == ["check_a.py"]

    def test_registry_only_orphan_is_named_as_such(self, tmp_path, capsys, monkeypatch):
        """The report says WHY: in TOOLS under key 'a', selected by nobody."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        self._registry(tmp_path, ["a"])
        monkeypatch.setattr(ol.Path, "resolve", lambda self_: self_, raising=False)
        # drive main() against the scaffold by pointing __file__ under it
        fake = tmp_path / "scripts" / "tools" / "lint" / "check_orphan_lint.py"
        monkeypatch.setattr(ol, "__file__", str(fake))
        monkeypatch.setattr("sys.argv", ["check_orphan_lint.py", "--ci"])
        rc = ol.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "check_frontmatter_versions" not in out
        assert "as 'a'" in out and "registry membership is not execution" in out

    def test_skips_venv_and_vendored_trees(self, tmp_path):
        """A check_*.py name appearing inside scripts/.venv or node_modules must
        NOT be scanned (local-dev dependency-black-hole guard)."""
        lint_dir = self._scaffold(tmp_path)
        (lint_dir / "check_a.py").write_text("", encoding="utf-8")
        for blackhole in (".venv", "venv", "node_modules", "__pycache__"):
            d = tmp_path / "scripts" / blackhole / "pkg"
            d.mkdir(parents=True)
            (d / "vendored.py").write_text("check_a.py\n", encoding="utf-8")
        refs = ol.gather_referencers(tmp_path, lint_dir)
        assert all(not (ol._SKIP_DIRS & set(p.parts)) for p in refs)
        # check_a is referenced ONLY inside the black holes → still orphan
        assert ol.find_orphans(["check_a.py"], ol.read_corpus(refs)) == \
            ["check_a.py"]

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
