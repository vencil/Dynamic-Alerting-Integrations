"""Tests for scripts/ops/recover_index.sh.

Covers:
  - --check on clean repo exits 0 with ✅
  - --check on corrupted index exits 2 with 🔴 signature
  - rebuild: corrupts real index, runs script, verifies git status works again
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "recover_index.sh"


def _init_tmp_repo(tmp_path: Path) -> Path:
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True
    )
    (tmp_path / "seed.txt").write_text("seed\n")
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        }
    )
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "add", "seed.txt"], check=True, env=env
    )
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )
    return tmp_path


def test_check_clean_exits_0(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "healthy" in proc.stdout.lower()


def test_check_corrupt_exits_2(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # Corrupt the index by overwriting with garbage that doesn't parse
    (repo / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x99garbage")

    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT), "--check"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, f"stdout={proc.stdout} stderr={proc.stderr}"
    assert "corruption" in proc.stdout.lower() or "🔴" in proc.stdout


def test_rebuild_recovers_corrupt_index(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # Corrupt
    (repo / ".git" / "index").write_bytes(b"DIRC\x00\x00\x00\x99garbage")

    # Sanity: git status should fail pre-rebuild
    pre = subprocess.run(  # subprocess-timeout: ignore
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert pre.returncode != 0

    # Run recovery
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout} stderr={proc.stderr}"
    assert "recovered" in proc.stdout.lower()

    # git status should work now
    post = subprocess.run(  # subprocess-timeout: ignore
        ["git", "status", "--short"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert post.returncode == 0


def test_rebuild_on_clean_is_noop(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "nothing to recover" in proc.stdout.lower()
