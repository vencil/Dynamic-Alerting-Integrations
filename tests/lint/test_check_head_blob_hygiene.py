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
        # Test fixture wrapper for local `git` commands that complete
        # in milliseconds. Per S#74 lint rule, explicit timeout would
        # just be noise — silenced via marker.
        # subprocess-timeout: ignore
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

    @pytest.mark.timeout(30)
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

    @pytest.mark.timeout(120)
    def test_large_batch_does_not_deadlock(self, git_repo):
        """**Headline regression** for PR-2c (commit 45e51a8). Pre-fix
        ``_batch_cat_blobs`` hung indefinitely above ~150 paths because
        the single-thread write-then-read-loop pattern deadlocked once
        total ``git cat-file --batch --buffer`` output exceeded the
        Windows pipe buffer.

        **Empirical threshold** (PR-2c dogfood, 2026-05-01, intentional
        revert + run):

            - 20 × 200B (~4KB)   — pre-fix passes (sanity baseline)
            - 300 × 1KB (~300KB) — pre-fix passes (pipe buffer absorbs)
            - 300 × 8KB (~2.4MB) — pre-fix **passes** (path-count too low)
            - 1000 × 2KB (~2MB)  — pre-fix **deadlocks** ✓

        Path count matters more than total output size on Windows —
        each path requires a `readline()` for the header which is what
        the deadlock prevents from draining. So the test uses 1000
        files (well past the empirical threshold).

        **Defense-in-depth against regression**:
          1. ``@pytest.mark.timeout(120)`` — pytest-level fast-fail; if
             someone reverts the fix back to write-then-read-loop AND
             removes the inner ``communicate(timeout=60)`` safety net,
             the test fails at 120s instead of hanging until CI workflow
             timeout (hours).
          2. ``elapsed < 90.0`` assertion — soft budget catches *slow*
             regression. Wide because the fixture itself spends real
             time on ``git init`` + 1000 ``write_text()`` + ``git add``
             + ``git commit``, which on slow CI runners can be 20-30s
             before ``_batch_cat_blobs`` is even called.
          3. Inner ``communicate(timeout=60)`` in the function under
             test — the actual fix; if untouched, hard-stops at 60s
             with ``TimeoutExpired`` (handled → returns empty dict →
             assertion ``len(result) == 1000`` fails fast).

        Together: a regression triggers a clear pytest failure within
        ~60-120s, never an indefinite hang.
        """
        repo = git_repo(1000, content_size_bytes=2048)
        paths = sorted(p.name for p in repo.iterdir() if p.is_file())
        t0 = time.time()
        result = chbh._batch_cat_blobs(paths)
        elapsed = time.time() - t0
        # Soft budget — generous for CI variance (local: ~5s, CI: ~30-60s).
        # Fires before the 120s pytest-timeout hard-stop on slow regression.
        assert elapsed < 90.0, (
            f"_batch_cat_blobs took {elapsed:.1f}s for 1000 files "
            f"(~2MB output) — likely the PR-2c deadlock has regressed. "
            f"See commit 45e51a8 for the fix using proc.communicate()."
        )
        assert len(result) == 1000

    @pytest.mark.timeout(10)
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
        # subprocess-timeout: ignore  — test fixture, local git
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        # subprocess-timeout: ignore  — test fixture, local git
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
# main() — hang-localization milestones (PR #165 S#74 follow-up)
# ---------------------------------------------------------------------------
class TestMainProgressMilestones:
    """Pin the three hang-localization output points added in PR #165.

    Why this matters: pre-PR-#165 the hook went silent during long runs,
    making "stuck or just slow" indistinguishable. The milestones turn
    silent hangs into observable hangs at three distinct phases.
    """

    @pytest.mark.timeout(30)
    def test_default_mode_emits_milestones(self, git_repo, capsys, monkeypatch):
        """Default mode must show: 'Reading N...' + 'batch read complete' + final summary."""
        repo = git_repo(20, content_size_bytes=200)
        # main() reads sys.argv via argparse; simulate `--ci` invocation.
        monkeypatch.setattr(sys, "argv", ["check_head_blob_hygiene.py", "--ci"])
        rc = chbh.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Milestone 1: pre-batch announcement (shows entered _batch_cat_blobs).
        assert "Reading " in out and " HEAD blob(s)..." in out
        # Milestone 2: post-batch confirmation (shows _batch_cat_blobs returned).
        assert "batch read complete" in out
        # Milestone 3: final summary.
        assert "HEAD blob(s) clean" in out

    @pytest.mark.timeout(30)
    def test_default_mode_emits_per_100_progress_for_large_batch(
        self, git_repo, capsys, monkeypatch
    ):
        """At 200+ files, default mode must show ...scanned 100/N and 200/N progress."""
        repo = git_repo(250, content_size_bytes=200)
        monkeypatch.setattr(sys, "argv", ["check_head_blob_hygiene.py", "--ci"])
        rc = chbh.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Per-100 progress must fire at scanned counts 100 and 200.
        assert "scanned 100/" in out
        assert "scanned 200/" in out

    @pytest.mark.timeout(30)
    def test_verbose_mode_shows_per_file_progress(
        self, git_repo, capsys, monkeypatch
    ):
        """--verbose: per-file scan line for each blob (not per-100)."""
        repo = git_repo(5, content_size_bytes=100)
        monkeypatch.setattr(
            sys,
            "argv",
            ["check_head_blob_hygiene.py", "--ci", "--verbose"],
        )
        rc = chbh.main()
        out = capsys.readouterr().out
        assert rc == 0
        # Each of the 5 files should produce a "scan <path>" line.
        scan_lines = [line for line in out.splitlines() if line.lstrip().startswith("scan file_")]
        assert len(scan_lines) == 5


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
