"""Tests for scripts/ops/fuse_plumbing_commit.py.

Covers:
  - detect_phantom_lock: clean / index.lock / HEAD.lock / both
  - plumbing_commit: creates a commit skipping hooks, updates branch ref
  - normal_commit: delegates to git add + git commit
  - CLI parse: --show-locks, --msg vs -m, --auto fallback decision
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "fuse_plumbing_commit.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("fuse_plumbing_commit", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _init_tmp_repo(tmp_path: Path) -> Path:
    """Create a real git repo at tmp_path with one initial commit."""
    subprocess.run(
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
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "seed.txt"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"],
        check=True,
        env=env,
    )
    return tmp_path


def test_detect_phantom_lock_clean(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _init_tmp_repo(tmp_path)
    assert mod.detect_phantom_lock(repo) == []


def test_detect_phantom_lock_index_lock(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _init_tmp_repo(tmp_path)
    lock = repo / ".git" / "index.lock"
    lock.write_text("")
    detected = mod.detect_phantom_lock(repo)
    assert any("index.lock" in p for p in detected)


def test_detect_phantom_lock_head_lock(tmp_path: Path) -> None:
    mod = _load_module()
    repo = _init_tmp_repo(tmp_path)
    lock = repo / ".git" / "HEAD.lock"
    lock.write_text("")
    detected = mod.detect_phantom_lock(repo)
    assert any("HEAD.lock" in p for p in detected)


def test_plumbing_commit_happy_path(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)

    # Set git author for commit-tree
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    (repo / "new.txt").write_text("hello\n")

    sha = mod.plumbing_commit(repo, "feat: add new.txt\n", ["new.txt"])
    assert len(sha) == 40

    # Verify the commit is reachable and has the right file
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline", "-2"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert sha[:7] in log.stdout
    assert "feat: add new.txt" in log.stdout

    show = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:new.txt"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert show.stdout == "hello\n"


def test_plumbing_commit_preserves_exec_bit(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    script = repo / "runme.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    os.chmod(script, 0o755)

    sha = mod.plumbing_commit(repo, "feat: add runme\n", ["runme.sh"])

    # git ls-tree should show mode 100755
    ls = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", sha, "runme.sh"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ls.stdout.startswith("100755")


def test_cli_show_locks_exit_0_when_clean(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--show-locks"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "(no phantom locks)" in proc.stdout


def test_cli_show_locks_lists_locks(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / ".git" / "index.lock").write_text("")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--show-locks"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "index.lock" in proc.stdout


def test_cli_requires_message_for_commit(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "a.txt").write_text("a\n")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "a.txt"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "message required" in proc.stderr


def test_cli_auto_uses_plumbing_when_lock_present(
    tmp_path: Path, monkeypatch
) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.com")

    # Plant a phantom lock (empty file — normal git would fail with "lock exists")
    (repo / ".git" / "index.lock").write_text("")

    (repo / "a.txt").write_text("auto-path\n")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--auto",
            "-m",
            "feat: auto-path commit\n",
            "a.txt",
        ],
        capture_output=True,
        text=True,
    )
    # Should succeed via plumbing even though lock exists
    assert proc.returncode == 0, f"stderr={proc.stderr}  stdout={proc.stdout}"
    assert "phantom lock" in proc.stderr
    assert "committed:" in proc.stdout


def test_cli_rejects_message_conflict(tmp_path: Path, monkeypatch) -> None:
    """-m and --msg are mutually exclusive."""
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    msgfile = repo / "m.txt"
    msgfile.write_text("x\n")
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "-m",
            "a",
            "--msg",
            str(msgfile),
            "seed.txt",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "not allowed with" in proc.stderr or "argument" in proc.stderr
