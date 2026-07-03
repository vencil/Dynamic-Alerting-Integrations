"""Unit tests for check_workflow_git_push_permissions.py.

Regression target: docs-ci.yaml's check-coverage job ran `git push || true`
with no `contents: write` permission anywhere, so every auto-commit push
silently failed for the file's entire history (found during PR #983 review).
This lint makes that class of bug loud instead of silent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_workflow_git_push_permissions as guard  # noqa: E402


def _write(tmp_path: Path, name: str, content: str) -> Path:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    return f


class TestLiveRepoIsClean:
    def test_real_workflows_dir_passes(self):
        """The shipped .github/workflows/ must have no ungranted `git push`.

        If this fails, someone re-introduced the docs-ci.yaml bug pattern
        (or a new one) — the regression target for this lint.
        """
        repo_root = guard._repo_root()
        violations = guard.scan_repo(repo_root / ".github" / "workflows")
        assert violations == [], (
            "Ungranted `git push` step(s) in .github/workflows/:\n  "
            + "\n  ".join(violations)
        )


class TestOriginalBugPattern:
    def test_no_permissions_block_at_all_is_flagged(self, tmp_path):
        """Exact shape of the original bug: no `permissions:` anywhere."""
        wf = _write(tmp_path, "bad.yaml", """
jobs:
  check-coverage:
    runs-on: ubuntu-latest
    steps:
      - name: Commit badge update
        run: |
          git add badge.json
          git commit -m "docs: update badge"
          git push || true
""")
        violations = guard.scan_workflow(wf)
        assert len(violations) == 1
        assert "Commit badge update" in violations[0]
        assert "implicit (repo default" in violations[0]

    def test_job_permissions_block_without_contents_key_does_not_inherit_top_level(
        self, tmp_path
    ):
        """GH Actions semantics: a job-level `permissions:` block, if present
        at all, REPLACES the workflow-level one wholesale — it does not merge
        per-scope. A job that declares `permissions: {issues: write}` gets an
        effective `contents: none`, even though the workflow grants
        `contents: write` at the top level. A checker that naively falls back
        to the top-level grant here would produce a false negative — worse
        than no checker, since it creates false confidence.
        """
        wf = _write(tmp_path, "bad.yaml", """
permissions:
  contents: write
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      issues: write
    steps:
      - name: Push changes
        run: git push
""")
        violations = guard.scan_workflow(wf)
        assert len(violations) == 1
        assert "'none'" in violations[0]

    def test_top_level_read_permission_is_flagged(self, tmp_path):
        """PR #983 shape: explicit top-level `contents: read` with no override."""
        wf = _write(tmp_path, "bad.yaml", """
permissions:
  contents: read
  pull-requests: read
jobs:
  check-coverage:
    runs-on: ubuntu-latest
    steps:
      - name: Commit badge update
        run: git push
""")
        violations = guard.scan_workflow(wf)
        assert len(violations) == 1
        assert "'read'" in violations[0]


class TestGrantedPushIsClean:
    def test_job_level_write_permission_passes(self, tmp_path):
        wf = _write(tmp_path, "good.yaml", """
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Push changes
        run: git push
""")
        assert guard.scan_workflow(wf) == []

    def test_top_level_write_permission_passes(self, tmp_path):
        wf = _write(tmp_path, "good.yaml", """
permissions:
  contents: write
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - name: Push changes
        run: git push origin main
""")
        assert guard.scan_workflow(wf) == []

    def test_write_all_shorthand_passes(self, tmp_path):
        wf = _write(tmp_path, "good.yaml", """
permissions: write-all
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - name: Push changes
        run: git push
""")
        assert guard.scan_workflow(wf) == []


class TestNoPushIsUnaffected:
    def test_workflow_without_git_push_is_ignored(self, tmp_path):
        wf = _write(tmp_path, "fine.yaml", """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build && git status
""")
        assert guard.scan_workflow(wf) == []

    def test_git_push_mentioned_only_in_a_comment_line_is_ignored(self, tmp_path):
        """Dogfooding regression: this hook's own pre-commit-config.yaml entry
        explains itself inside a `run: |` block via `#` comment lines, which
        the YAML parser treats as literal string content, not a real
        `git push`. A naive substring/regex scan would false-positive on its
        own explanatory text.
        """
        wf = _write(tmp_path, "fine.yaml", """
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Run pre-commit hooks
        run: |
          pre-commit run some-check --all-files
          # a `git push` step without contents: write silently no-ops
          pre-commit run workflow-git-push-permission-check --all-files
""")
        assert guard.scan_workflow(wf) == []
