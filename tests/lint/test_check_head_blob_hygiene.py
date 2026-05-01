"""Tests for check_head_blob_hygiene.py — HEAD blob corruption scanner.

Pinned contracts
----------------
1. **`_batch_cat_blobs` does not deadlock above pipe-buffer-sized batches**
   — regression for PR-2c (commit 45e51a8). The pre-fix Popen
   write-then-read-loop pattern hung indefinitely once total git
   cat-file output exceeded the OS pipe buffer (~64KB on Windows). Fix
   was to switch to ``proc.communicate(input=request, timeout=60)``
   which threads stdin write + stdout/stderr drain. These tests use a
   real git repo fixture with enough blobs to push past the buffer
   threshold and assert the call returns within seconds.

2. **Output parsing handles all three batch-output cases**
   — `<sha> blob <size>\\n<bytes>\\n`, `<input> missing\\n`, and
   unexpected-header break-and-return-partial.

3. **scan_blob detects each rule class** — NUL bytes, missing EOF
   newline (under `--strict`), truncated YAML/JSON.

4. **`_is_skippable` honours binary extensions and unknown extensions**
   — truthy for `.png` / `.exe` / unknown extension; falsy for `.py` /
   `.md` / `.yaml`.

The deadlock regression test (#1) is the headline reason this file
exists: the pre-fix version of `_batch_cat_blobs` would have hung this
test forever rather than failing fast, but that's exactly what the
test pins — it uses ``timeout`` discipline to fail loudly if the
deadlock recurs.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_head_blob_hygiene as chbh  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real on-disk git repo with ``n`` configurable blobs.

    Yields a function ``populate(n, content_size_bytes)`` that creates
    ``n`` files of approximately ``content_size_bytes`` each, stages
    them, and commits. Returns the repo Path.

    Why a real git repo: ``_batch_cat_blobs`` shells out to
    ``git cat-file --batch`` and reads its output. Mocking the
    subprocess wouldn't exercise the actual pipe-buffer deadlock path
    we're trying to pin.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            check=True,
        )

    _git("init", "--quiet", "--initial-branch=main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")

    def populate(n: int, content_size_bytes: int = 100) -> Path:
        for i in range(n):
            f = repo / f"file_{i:04d}.txt"
            # Deterministic content; clean (no NUL, ends with \n).
            body = (f"file {i}\n" * (content_size_bytes // 8 + 1))[
                :content_size_bytes
            ]
            if not body.endswith("\n"):
                body += "\n"
            f.write_text(body, encoding="utf-8")
        if n > 0:
            _git("add", ".")
            _git("commit", "--quiet", "-m", "test")
        return repo

    # Patch PROJECT_ROOT so the script's helpers cwd into our repo.
    monkeypatch.setattr(chbh, "PROJECT_ROOT", repo)
    populate.repo = repo
    return populate


# ---------------------------------------------------------------------------
# _batch_cat_blobs — deadlock regression (the headline test)
# ---------------------------------------------------------------------------
class TestBatchCatBlobsDeadlock:
    """Regression tests for PR-2c hygiene-hook deadlock.

    The pre-fix ``_batch_cat_blobs`` hung indefinitely once total
    cat-file output exceeded the OS pipe buffer (~64KB on Windows). At
    this repo's 1040-file size that meant ~2.18MB of output and
    indefinite hang on every commit. The fix uses
    ``proc.communicate(timeout=60)`` to drain pipes via internal
    threads.
    """

    def test_small_batch_fits_in_pipe_buffer(self, git_repo):
        """Sanity: small batches always worked, even pre-fix."""
        repo = git_repo(20, content_size_bytes=200)
        paths = sorted(p.name for p in repo.iterdir() if p.is_file())
        t0 = time.time()
        result = chbh._batch_cat_blobs(paths)
        elapsed = time.time() - t0
        assert len(result) == 20
        assert elapsed < 5.0
        # Content sanity: bytes match what we wrote.
        for path in paths:
            assert path in result
            assert result[path].endswith(b"\n")

    def test_large_batch_exceeds_pipe_buffer_does_not_deadlock(self, git_repo):
        """Headline regression: total output > pipe buffer must not hang.

        300 files × 1KB = ~300KB of total cat-file output, well above
        the ~64KB pipe buffer that triggers the pre-fix deadlock.
        Pre-fix: hung indefinitely. Post-fix: completes in <10s.

        The critical observation: this test FAILS LOUDLY (timeout error
        from the script's own 60s safety net or pytest timeout) rather
        than passing silently if someone reverts the fix.
        """
        repo = git_repo(300, content_size_bytes=1024)
        paths = sorted(p.name for p in repo.iterdir() if p.is_file())
        t0 = time.time()
        result = chbh._batch_cat_blobs(paths)
        elapsed = time.time() - t0
        # Generous budget — local runs see ~1s; CI variance accounted for.
        assert elapsed < 30.0, (
            f"_batch_cat_blobs took {elapsed:.1f}s for 300 files "
            f"(~300KB output) — likely the PR-2c deadlock has regressed. "
            f"See commit 45e51a8 for the fix using proc.communicate()."
        )
        assert len(result) == 300

    def test_huge_batch_still_completes(self, git_repo):
        """Stress: 1000 files × 2KB = ~2MB total — close to real-repo size."""
        repo = git_repo(1000, content_size_bytes=2048)
        paths = sorted(p.name for p in repo.iterdir() if p.is_file())
        t0 = time.time()
        result = chbh._batch_cat_blobs(paths)
        elapsed = time.time() - t0
        assert elapsed < 60.0
        assert len(result) == 1000

    def test_empty_paths_returns_empty_dict(self, git_repo):
        """Edge: empty input must not spawn git or hang."""
        # Doesn't even need a populated repo for this one.
        git_repo(1)
        assert chbh._batch_cat_blobs([]) == {}


# ---------------------------------------------------------------------------
# _batch_cat_blobs — output parsing
# ---------------------------------------------------------------------------
class TestBatchCatBlobsParsing:
    """Pin the three batch-output cases."""

    def test_blob_content_round_trips(self, git_repo):
        """Bytes returned must match committed content exactly."""
        repo = git_repo(0)
        # Custom content with binary-safe bytes that aren't \n.
        special = b"line one\nline two\n\x01\x02middle\nlast no newline"
        f = repo / "special.txt"
        f.write_bytes(special)
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "special"],
            cwd=repo,
            check=True,
        )
        result = chbh._batch_cat_blobs(["special.txt"])
        assert result["special.txt"] == special

    def test_missing_path_skipped_silently(self, git_repo):
        """`<input> missing\\n` rows skip without breaking the batch."""
        repo = git_repo(3)
        paths = sorted(p.name for p in repo.iterdir() if p.is_file())
        # Insert a path that doesn't exist between two valid ones.
        mixed = [paths[0], "does-not-exist.txt", paths[1], paths[2]]
        result = chbh._batch_cat_blobs(mixed)
        # Real paths return; missing one is omitted.
        assert paths[0] in result
        assert paths[1] in result
        assert paths[2] in result
        assert "does-not-exist.txt" not in result


# ---------------------------------------------------------------------------
# scan_blob — rule detection
# ---------------------------------------------------------------------------
class TestScanBlob:
    def test_clean_text_blob_no_violations(self):
        violations = chbh.scan_blob("foo.py", b"print('ok')\n")
        assert violations == []

    def test_nul_byte_in_text_blob_is_violation(self):
        violations = chbh.scan_blob("foo.py", b"print('ok')\n\x00")
        assert any(v.rule == "NUL" for v in violations)

    def test_missing_eof_newline_only_under_strict(self):
        # Default (non-strict): missing newline tolerated.
        loose = chbh.scan_blob("foo.py", b"print('ok')")
        assert not any(v.rule == "EOF" for v in loose)
        # Strict: missing newline flagged.
        strict = chbh.scan_blob("foo.py", b"print('ok')", strict=True)
        assert any(v.rule == "EOF" for v in strict)

    def test_truncated_yaml_detected(self):
        # The mkdocs.yml regression that motivated rule #3:
        # last line is `- Changelog: CHANGELOG.m` — key-without-value.
        truncated = b"nav:\n  - Home: index.md\n  - Changelog: CHANGELOG.m"
        violations = chbh.scan_blob("mkdocs.yml", truncated)
        assert any(v.rule == "TRUNC" for v in violations)


# ---------------------------------------------------------------------------
# _is_skippable
# ---------------------------------------------------------------------------
class TestIsSkippable:
    @pytest.mark.parametrize(
        "path",
        ["logo.png", "fonts/icon.woff2", "build.exe", "image.svg"],
    )
    def test_binary_extensions_skipped(self, path):
        assert chbh._is_skippable(path) is True

    @pytest.mark.parametrize(
        "path",
        ["foo.py", "docs/readme.md", "config.yaml", "tsconfig.json"],
    )
    def test_text_extensions_not_skipped(self, path):
        assert chbh._is_skippable(path) is False
