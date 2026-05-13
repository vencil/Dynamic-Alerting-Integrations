"""Tests for scripts/dx/generate_planning_index.py (#379 chunk 2a).

Pinned contracts
----------------
1. **Source 1 — top-of-file frontmatter**: every `docs/**/*.md` file is checked; if its
   frontmatter has `tracking_kind:` in the enum AND `id:` AND `status:`, it becomes one
   entry. Missing any required field skips silently (no halt).

2. **Source 2 — embedded YAML code blocks**: an H2/H3 heading immediately followed by a
   triple-backtick yaml fence containing the required fields produces one entry per
   match, with `source_line` pointing at the heading.

3. **Source 3 — flaky-tests.yaml**: top-level list items with `tracking_kind:` become
   entries; existing entries with only `tracked_by:` (the legacy field) are skipped.

4. **Source 4 — code-comment annotations**: `// TECH-DEBT(id=..., status=..., tracking_kind=...)`
   or `# TECH-DEBT(...)` ONLY when the comment marker is the first non-whitespace token on
   its line. Prose / docstring / code-span mentions intentionally ignored.

5. **Sort + render**: entries grouped by status in STATUS_ORDER, then sorted by
   (tracking_kind, id). Empty entries → friendly placeholder text (not a malformed table).

6. **Drift gate**: `--check` exits 1 when rendered output differs; `--write` is
   idempotent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_DX_DIR = REPO_ROOT / "scripts" / "dx"
sys.path.insert(0, str(_DX_DIR))

import generate_planning_index as gpi  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_doc(path: Path, frontmatter: dict, body: str = "") -> None:
    """Write a docs/**/*.md with the given frontmatter dict + optional body."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        fm_lines.append(f"{k}: {v}")
    fm_lines.append("---")
    text = "\n".join(fm_lines) + "\n\n" + body + "\n"
    path.write_text(text, encoding="utf-8")


def _make_target(path: Path) -> None:
    """Write the target doc skeleton with the sentinel block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Planning Index\n\n"
        f"{gpi.SENTINEL_START}\n"
        "placeholder\n"
        f"{gpi.SENTINEL_END}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Source 1 — top-of-file frontmatter
# ---------------------------------------------------------------------------
class TestDocFrontmatterDiscovery:
    def test_valid_entry_picked_up(self, tmp_path):
        _make_doc(
            tmp_path / "docs" / "foo.md",
            {
                "title": '"Test Entry"',
                "id": "TRK-300",
                "tracking_kind": "tech-debt",
                "status": "in-progress",
                "domain": "tenant-api",
            },
        )
        entries = list(gpi._discover_doc_frontmatter(tmp_path))
        assert len(entries) == 1
        e = entries[0]
        assert e.id == "TRK-300"
        assert e.title == "Test Entry"
        assert e.tracking_kind == "tech-debt"
        assert e.status == "in-progress"
        assert e.domain == "tenant-api"
        assert e.source_path == "docs/foo.md"
        assert e.source_line == 0  # top-of-file frontmatter has no line anchor

    def test_missing_tracking_kind_skipped(self, tmp_path):
        _make_doc(
            tmp_path / "docs" / "foo.md",
            {"title": '"x"', "id": "TRK-301", "status": "proposed"},  # no tracking_kind
        )
        assert list(gpi._discover_doc_frontmatter(tmp_path)) == []

    def test_invalid_tracking_kind_skipped(self, tmp_path):
        _make_doc(
            tmp_path / "docs" / "foo.md",
            {
                "title": '"x"',
                "id": "TRK-302",
                "tracking_kind": "made-up-kind",  # not in enum
                "status": "proposed",
            },
        )
        assert list(gpi._discover_doc_frontmatter(tmp_path)) == []

    def test_invalid_status_skipped(self, tmp_path):
        _make_doc(
            tmp_path / "docs" / "foo.md",
            {
                "id": "TRK-303",
                "tracking_kind": "dx",
                "status": "wat",  # not in enum
            },
        )
        assert list(gpi._discover_doc_frontmatter(tmp_path)) == []

    def test_missing_id_skipped(self, tmp_path):
        _make_doc(
            tmp_path / "docs" / "foo.md",
            {"tracking_kind": "adr", "status": "accepted"},
        )
        assert list(gpi._discover_doc_frontmatter(tmp_path)) == []

    def test_malformed_yaml_skipped_not_raised(self, tmp_path):
        p = tmp_path / "docs" / "broken.md"
        p.parent.mkdir(parents=True)
        p.write_text("---\nid: TRK-1\n  bad: yaml: here\n---\n", encoding="utf-8")
        # Should not raise; should skip the entry.
        assert list(gpi._discover_doc_frontmatter(tmp_path)) == []


# ---------------------------------------------------------------------------
# Source 2 — embedded YAML code blocks
# ---------------------------------------------------------------------------
class TestSectionYamlDiscovery:
    def test_section_yaml_block_picked_up(self, tmp_path):
        p = tmp_path / "docs" / "backlog.md"
        p.parent.mkdir(parents=True)
        p.write_text(
            textwrap.dedent(
                """\
                # Backlog

                ## TRK-400: First item

                ```yaml
                id: TRK-400
                tracking_kind: dx
                status: proposed
                ```

                Description.

                ## TRK-401: Second item

                ```yaml
                id: TRK-401
                tracking_kind: feature
                status: in-progress
                pr_ref: 472
                ```
                """
            ),
            encoding="utf-8",
        )
        entries = sorted(
            gpi._discover_section_yaml(tmp_path), key=lambda e: e.id,
        )
        assert [e.id for e in entries] == ["TRK-400", "TRK-401"]
        assert entries[0].title == "TRK-400: First item"
        assert entries[1].pr_ref == "472"
        assert entries[0].source_line > 0
        assert entries[1].source_line > entries[0].source_line

    def test_section_yaml_without_tracking_kind_skipped(self, tmp_path):
        p = tmp_path / "docs" / "backlog.md"
        p.parent.mkdir(parents=True)
        p.write_text(
            "## Section\n\n```yaml\nid: TRK-500\nstatus: proposed\n```\n",
            encoding="utf-8",
        )
        assert list(gpi._discover_section_yaml(tmp_path)) == []


# ---------------------------------------------------------------------------
# Source 3 — flaky-tests.yaml
# ---------------------------------------------------------------------------
class TestFlakyTestsDiscovery:
    def test_entry_with_tracking_kind(self, tmp_path):
        p = tmp_path / "flaky-tests.yaml"
        p.write_text(
            textwrap.dedent(
                """\
                - test: TestFooFlaky
                  pattern: ^TestFooFlaky$
                  id: TRK-600
                  tracking_kind: regression
                  status: accepted
                  max_retries: 2
                """
            ),
            encoding="utf-8",
        )
        entries = list(gpi._discover_flaky_tests(p, tmp_path))
        assert len(entries) == 1
        e = entries[0]
        assert e.id == "TRK-600"
        assert e.tracking_kind == "regression"
        assert e.status == "accepted"

    def test_legacy_entry_without_tracking_kind_skipped(self, tmp_path):
        p = tmp_path / "flaky-tests.yaml"
        p.write_text(
            "- test: LegacyFlake\n  pattern: ^Legacy$\n  tracked_by: HA-10\n",
            encoding="utf-8",
        )
        assert list(gpi._discover_flaky_tests(p, tmp_path)) == []

    def test_missing_file_yields_nothing(self, tmp_path):
        assert list(gpi._discover_flaky_tests(tmp_path / "missing.yaml", tmp_path)) == []


# ---------------------------------------------------------------------------
# Source 4 — code-comment annotations
# ---------------------------------------------------------------------------
class TestCodeAnnotationDiscovery:
    def test_line_start_comment_picked_up(self, tmp_path):
        p = tmp_path / "tools" / "thing.ts"
        p.parent.mkdir(parents=True)
        p.write_text(
            "function foo() {}\n"
            "// TECH-DEBT(id=TRK-700, status=in-progress, tracking_kind=tech-debt)\n"
            "function bar() {}\n",
            encoding="utf-8",
        )
        entries = list(gpi._discover_code_annotations(tmp_path))
        assert len(entries) == 1
        e = entries[0]
        assert e.id == "TRK-700"
        assert e.source_path == "tools/thing.ts"
        assert e.source_line == 2

    def test_indented_comment_still_matches(self, tmp_path):
        p = tmp_path / "scripts" / "thing.py"
        p.parent.mkdir(parents=True)
        p.write_text(
            "def f():\n"
            "    # TECH-DEBT(id=TRK-701, status=accepted, tracking_kind=dx)\n"
            "    return 1\n",
            encoding="utf-8",
        )
        entries = list(gpi._discover_code_annotations(tmp_path))
        assert len(entries) == 1
        assert entries[0].id == "TRK-701"

    def test_prose_or_docstring_mention_ignored(self, tmp_path):
        """The crucial false-positive guard. Without ^\\s* anchor, this file would
        register as a planning entry from its own docstring."""
        p = tmp_path / "scripts" / "thing.py"
        p.parent.mkdir(parents=True)
        p.write_text(
            '"""Docstring describing the format:\n'
            "    The annotation looks like `// TECH-DEBT(id=TRK-999, status=in-progress, tracking_kind=tech-debt)`.\n"
            '"""\n'
            "def f(): return 1\n",
            encoding="utf-8",
        )
        assert list(gpi._discover_code_annotations(tmp_path)) == []

    def test_inline_code_span_in_markdown_safe(self, tmp_path):
        """Prose like 'such as `// TECH-DEBT(...)`' inside backticks should not match
        because the comment marker is preceded by a non-whitespace char (the backtick)."""
        p = tmp_path / "scripts" / "thing.py"
        p.parent.mkdir(parents=True)
        p.write_text(
            "# header_comment\n"
            "msg = 'such as `// TECH-DEBT(id=TRK-998, status=done, tracking_kind=tech-debt)`'\n",
            encoding="utf-8",
        )
        assert list(gpi._discover_code_annotations(tmp_path)) == []


# ---------------------------------------------------------------------------
# Render + sentinel replacement
# ---------------------------------------------------------------------------
class TestRender:
    def test_empty_entries_render_placeholder(self):
        out = gpi.render_index([])
        assert "目前 discovery 沒有任何 entry" in out
        assert "| ID |" not in out  # no table header when empty

    def test_grouped_by_status_in_status_order(self):
        entries = [
            gpi.PlanningEntry(
                id="TRK-1", title="A", tracking_kind="tech-debt", status="done",
                source_path="docs/a.md",
            ),
            gpi.PlanningEntry(
                id="TRK-2", title="B", tracking_kind="dx", status="in-progress",
                source_path="docs/b.md",
            ),
            gpi.PlanningEntry(
                id="TRK-3", title="C", tracking_kind="adr", status="proposed",
                source_path="docs/c.md",
            ),
        ]
        out = gpi.render_index(entries)
        # in-progress group must come before proposed which must come before done.
        ip = out.index("in-progress")
        pr = out.index("proposed")
        dn = out.index("done")
        assert ip < pr < dn

    def test_sentinel_replacement_idempotent(self):
        content = (
            f"prefix\n{gpi.SENTINEL_START}\nold\n{gpi.SENTINEL_END}\nsuffix\n"
        )
        once = gpi.replace_sentinel_block(content, "fresh\n")
        twice = gpi.replace_sentinel_block(once, "fresh\n")
        assert once == twice

    def test_missing_sentinel_raises(self):
        with pytest.raises(ValueError, match="Sentinel block missing"):
            gpi.replace_sentinel_block("no markers", "body\n")


# ---------------------------------------------------------------------------
# End-to-end CLI
# ---------------------------------------------------------------------------
class TestMainCli:
    @pytest.fixture
    def tiny_repo(self, tmp_path):
        # docs/ entry with full frontmatter
        _make_doc(
            tmp_path / "docs" / "trk-800.md",
            {
                "title": '"Sample Entry"',
                "id": "TRK-800",
                "tracking_kind": "tech-debt",
                "status": "in-progress",
            },
        )
        target = tmp_path / "docs" / "internal" / "planning-index.md"
        _make_target(target)
        return tmp_path, target

    def _run(self, root, *args, expect_returncode=0):
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.run(
            [
                sys.executable,
                str(_DX_DIR / "generate_planning_index.py"),
                "--repo-root", str(root),
                "--target", str(args[-1]),
                *args[:-1],
            ],
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
        root, target = tiny_repo
        proc = self._run(root, "--check", target, expect_returncode=1)
        assert "DRIFT" in proc.stderr
        assert "make planning-index" in proc.stderr

    def test_write_then_check_clean(self, tiny_repo):
        root, target = tiny_repo
        self._run(root, "--write", target)
        body = target.read_text(encoding="utf-8")
        assert "TRK-800" in body
        assert "Sample Entry" in body
        self._run(root, "--check", target)  # exit 0

    def test_write_is_idempotent(self, tiny_repo):
        root, target = tiny_repo
        self._run(root, "--write", target)
        once = target.read_text(encoding="utf-8")
        proc = self._run(root, "--write", target)
        assert "no change" in proc.stdout
        assert target.read_text(encoding="utf-8") == once

    def test_write_uses_lf_line_endings(self, tiny_repo):
        """Mirrors the PR #477 regression — atomic_write_text must force LF."""
        root, target = tiny_repo
        self._run(root, "--write", target)
        raw = target.read_bytes()
        assert b"\r\n" not in raw

    def test_missing_sentinel_returns_2(self, tiny_repo):
        root, target = tiny_repo
        target.write_text("no sentinel here\n", encoding="utf-8")
        proc = self._run(root, "--check", target, expect_returncode=2)
        assert "Sentinel block missing" in proc.stderr


# ---------------------------------------------------------------------------
# Real-corpus smoke — guards the live drift gate against parse regressions
# ---------------------------------------------------------------------------
class TestRealCorpus:
    def test_discover_all_succeeds(self):
        """Whatever the live corpus contains, discovery must complete without raising."""
        entries = gpi.discover_all()
        # Every yielded entry must satisfy the contract.
        for e in entries:
            assert e.id, f"empty id: {e}"
            assert e.tracking_kind in gpi.TRACKING_KINDS, f"bad kind: {e}"
            assert e.status in gpi.STATUSES, f"bad status: {e}"

    def test_live_target_doc_in_sync(self):
        """If this fails, run `make planning-index` to refresh."""
        entries = gpi.discover_all()
        body = gpi.render_index(entries)
        current = gpi.TARGET_DOC.read_text(encoding="utf-8")
        new = gpi.replace_sentinel_block(current, body)
        assert new == current, (
            "planning-index.md is stale. Run `make planning-index` to refresh."
        )
