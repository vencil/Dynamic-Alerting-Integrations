#!/usr/bin/env python3
"""test_git_check_lock — covers PR #52 ADD-1 hardening of
`scripts/session-guards/git_check_lock.sh`:

  - Trap #58 codify: self-PID + parent-PID filter so Makefile-spawned
    bash subshell is not mis-counted as "active git"
  - Trap #59 codify: .git/HEAD NUL-byte corruption detection + auto-
    repair in `--clean` mode; dedicated `--check-head` exit-code
    contract

All tests fabricate a minimal `.git/` layout in `tmp_path` and invoke
the shell script via subprocess. No reliance on the real repo state.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "session-guards"
    / "git_check_lock.sh"
)


# ── helpers ────────────────────────────────────────────────────────────

# ⛔ Resolved on PATH, not passed bare (#2328): on Windows CreateProcess
# searches System32 before PATH, so a bare "bash" is WSL's bash.exe, and the
# script ran under WSL -- with pgrep, and with a Linux git that cannot read a
# linked worktree's `gitdir: C:/...`. The script's Windows users run Git Bash.
_BASH = shutil.which("bash") or "bash"


def _make_fake_repo(
    tmp_path: Path,
    *,
    head_content: bytes = b"ref: refs/heads/main\n",
    lock_files: tuple[str, ...] = (),
    lock_age_seconds: int = 0,
    reflog: str | None = None,
) -> Path:
    """Build a minimal .git/ layout under tmp_path sufficient for the
    script's `find .git -name "*.lock"` + `stat` + HEAD parsing calls."""
    git = tmp_path / ".git"
    git.mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_bytes(head_content)
    # Satisfy `git rev-parse --show-toplevel` inside the script.
    # The script falls back to cwd if rev-parse fails, so we don't
    # actually need a full git init for most tests. But to make the
    # subprocess predictable, pass cwd=tmp_path so rev-parse returns 1
    # and the script falls back cleanly.
    refs_heads = git / "refs" / "heads"
    refs_heads.mkdir(parents=True, exist_ok=True)
    (refs_heads / "main").write_text("0" * 40 + "\n", encoding="utf-8")

    if reflog is not None:
        logs = git / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "HEAD").write_text(reflog, encoding="utf-8")

    for lock_name in lock_files:
        lock_path = git / lock_name
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("", encoding="utf-8")
        if lock_age_seconds > 0:
            age_ago = time.time() - lock_age_seconds
            os.utime(lock_path, (age_ago, age_ago))

    return tmp_path


def _run_script(
    repo_root: Path,
    *args: str,
    timeout_s: float = 10.0,
) -> subprocess.CompletedProcess:
    """Invoke the shell script in a way that works on both Linux and
    Windows Git Bash.

    Strategy: copy the script into `repo_root` and invoke by bare name,
    which avoids the Windows-vs-MSYS path translation friction entirely
    (Python subprocess on Windows doesn't set up the MSYS environment,
    so neither 'C:/...' nor '/c/...' reliably works when passed as
    bash's argv[1]).
    """
    script_copy = repo_root / "_git_check_lock.sh"
    shutil.copy(SCRIPT_PATH, script_copy)
    try:
        return subprocess.run(
            [_BASH, "./_git_check_lock.sh", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    finally:
        # Cleanup copy so the .git/* walk isn't polluted between tests
        try:
            script_copy.unlink()
        except OSError:
            pass


# ── TestHeadSanity ─────────────────────────────────────────────────────


class TestHeadSanity:
    """Trap #59 codify: HEAD NUL-byte corruption detection."""

    def test_normal_head_passes(self, tmp_path):
        _make_fake_repo(tmp_path)
        result = _run_script(tmp_path, "--check-head")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "正常" in result.stdout

    def test_detached_head_with_hex_sha_is_sane(self, tmp_path):
        _make_fake_repo(
            tmp_path,
            head_content=b"a" * 40 + b"\n",
        )
        result = _run_script(tmp_path, "--check-head")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_head_with_nul_fill_detected(self, tmp_path):
        # Simulates FUSE cache loss: 45-byte valid content padded with
        # NUL to 90 bytes.
        valid = b"ref: refs/heads/main\n"
        padded = valid + b"\x00" * (90 - len(valid))
        _make_fake_repo(tmp_path, head_content=padded)
        result = _run_script(tmp_path, "--check-head")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "NUL byte" in result.stdout

    def test_head_with_garbage_first_line(self, tmp_path):
        _make_fake_repo(tmp_path, head_content=b"not a valid HEAD line\n")
        result = _run_script(tmp_path, "--check-head")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "首行格式異常" in result.stdout

    def test_head_auto_repair_on_clean(self, tmp_path):
        """--clean should rewrite NUL-padded HEAD back to clean ref."""
        valid = b"ref: refs/heads/feat-xyz\n"
        padded = valid + b"\x00" * 60
        reflog = (
            "0" * 40 + " " + "a" * 40
            + " User <u@x> 1700000000 +0000\tcheckout: "
            + "moving from main to feat-xyz\n"
        )
        _make_fake_repo(
            tmp_path,
            head_content=padded,
            reflog=reflog,
        )
        result = _run_script(tmp_path, "--clean")
        # Auto-repair should succeed; overall exit 0
        assert result.returncode == 0, result.stdout + result.stderr
        # File on disk should now be clean
        repaired = (tmp_path / ".git" / "HEAD").read_bytes()
        assert b"\x00" not in repaired
        assert repaired == b"ref: refs/heads/feat-xyz\n"

    def test_head_unrecoverable_without_reflog_or_single_branch(
        self, tmp_path
    ):
        """If reflog missing and multiple branches, recovery fails (exit 2)."""
        valid = b"ref: refs/heads/main\n"
        padded = valid + b"\x00" * 60
        _make_fake_repo(tmp_path, head_content=padded)
        # Add a second branch so single-branch heuristic fails too
        (tmp_path / ".git" / "refs" / "heads" / "other").write_text(
            "0" * 40 + "\n", encoding="utf-8"
        )
        result = _run_script(tmp_path, "--clean")
        assert result.returncode == 2, result.stdout + result.stderr
        assert "無法從 reflog" in result.stdout or "HEAD 仍未修復" in result.stdout


# ── TestSelfPIDFilter ──────────────────────────────────────────────────


class TestSelfPIDFilter:
    """Trap #58 codify: self + parent PID must not register as 'active git'."""

    def test_no_false_positive_when_invoked_normally(self, tmp_path):
        """The script itself and its parent bash shell must not count
        as 'active git' even though both have 'git' in argv."""
        _make_fake_repo(
            tmp_path,
            lock_files=("index.lock",),
            lock_age_seconds=60,  # stale enough to trigger cleanup
        )
        result = _run_script(tmp_path, "--clean")
        # The active-git list is information only since #2328 (nothing is
        # deleted), but a script that lists itself would still mislead.
        assert "有活躍的 git 程序" not in result.stdout, (
            "self-PID filter failed: script counted itself as active git.\n"
            + result.stdout
        )
        assert "殘留嫌疑的 lock" in result.stdout, result.stdout

    def test_filter_regex_excludes_self_and_parent(self, tmp_path):
        """Smoke: verify the printed 'active git' list excludes our own
        bash subprocess entry."""
        _make_fake_repo(tmp_path)
        result = _run_script(tmp_path)
        # In normal mode, the 'active git' section always runs. Our own
        # pid / parent pid should not appear.
        # This is a weak test (relies on bash argv format) but catches
        # obvious regressions.
        assert "git_check_lock.sh" not in result.stdout.split(
            "活躍的 Git 程序"
        )[-1].split("清理")[0] if "活躍的 Git 程序" in result.stdout else True


# ── TestLockDiagnosis (existing behaviour regression) ─────────────────


class TestLockDiagnosis:
    """Regression tests for existing lock-handling behaviour —
    PR #52 must not break what PR #44 established."""

    def test_no_locks_exits_zero(self, tmp_path):
        _make_fake_repo(tmp_path)
        result = _run_script(tmp_path)
        assert result.returncode == 0
        assert "沒有發現 lock" in result.stdout

    def test_fresh_lock_not_stale(self, tmp_path):
        _make_fake_repo(
            tmp_path,
            lock_files=("index.lock",),
            lock_age_seconds=5,  # fresh
        )
        result = _run_script(tmp_path)
        assert result.returncode == 0
        # Fresh lock printed as 🟡 (possibly in use), not 🔴 (stale)
        assert "🟡" in result.stdout

    def test_stale_lock_marked_red(self, tmp_path):
        _make_fake_repo(
            tmp_path,
            lock_files=("index.lock",),
            lock_age_seconds=60,
        )
        result = _run_script(tmp_path)
        assert result.returncode == 0
        assert "🔴" in result.stdout

    def test_clean_mode_lists_stale_locks_without_deleting(self, tmp_path):
        """#2328: a crashed git's lock and a running git's lock look the same."""
        lock = tmp_path / ".git" / "index.lock"
        _make_fake_repo(
            tmp_path,
            lock_files=("index.lock",),
            lock_age_seconds=60,
        )
        result = _run_script(tmp_path, "--clean")
        assert result.returncode == 0
        assert lock.exists(), "--clean deleted a lock:\n" + result.stdout
        assert "rm -f" in result.stdout and "index.lock" in result.stdout, result.stdout


# ── #2328: real git — held locks, linked worktrees ────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True,
        text=True, encoding="utf-8", timeout=60,
    ).stdout.strip()


def _real_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, timeout=60)
    _git(repo, "config", "user.email", "fixture@example.invalid")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "test: base")
    return repo


def _age(path: Path, seconds: int) -> None:
    then = time.time() - seconds
    os.utime(path, (then, then))


class TestLocksAreNeverDeleted:
    def test_a_lock_held_by_a_running_git_survives_clean(self, tmp_path):
        """The #2328 measurement: Git Bash has no pgrep, the old script read
        "can't tell" as "no git running" and deleted a lock git was holding."""
        repo = _real_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        # Bytes, not text: text mode on Windows writes `\r\n`, and git reads
        # `start\r` as an unknown command and exits.
        proc = subprocess.Popen(
            ["git", "update-ref", "--stdin"], cwd=repo, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            proc.stdin.write(f"start\ncreate refs/heads/held {head}\nprepare\n".encode())
            proc.stdin.flush()
            assert proc.stdout.readline().strip() == b"start: ok"
            assert proc.stdout.readline().strip() == b"prepare: ok"
            lock = repo / ".git" / "refs" / "heads" / "held.lock"
            assert lock.exists(), "control: the transaction holds no lock"
            _age(lock, 120)
            result = _run_script(repo, "--clean")
            assert lock.exists(), "--clean deleted a lock a running git holds:\n" + result.stdout
            assert proc.poll() is None, "control: the holder exited during the check"
            assert "held.lock" in result.stdout, result.stdout
        finally:
            proc.stdin.write(b"abort\n")
            proc.stdin.close()
            proc.wait(timeout=30)


class TestLockLocation:
    @staticmethod
    def _plant(tree: Path) -> list[str]:
        """Locks where git keeps them: this tree's index, and the common dir."""
        names = []
        for rel in ("index.lock", "packed-refs.lock"):
            p = Path(_git(tree, "rev-parse", "--path-format=absolute", "--git-path", rel))
            p.write_text("", encoding="utf-8")
            _age(p, 60)
            # Last two components: `linked/index.lock` names the linked tree's
            # own index, not the main tree's.
            names.append("/".join(p.as_posix().split("/")[-2:]))
        return names

    def test_linked_worktree_lists_its_locks(self, tmp_path):
        """A linked tree's `.git` is a file: the old `find <tree>/.git` saw no
        lock at all and reported everything fine."""
        repo = _real_repo(tmp_path)
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "feat", str(linked))
        names = self._plant(linked)
        result = _run_script(linked)
        assert "一切正常" not in result.stdout, result.stdout
        for name in names:
            assert name in result.stdout, f"{name} not listed:\n{result.stdout}"

    def test_main_tree_lists_its_locks(self, tmp_path):
        """Must-not-ring control: the main tree was already covered."""
        repo = _real_repo(tmp_path)
        names = self._plant(repo)
        result = _run_script(repo)
        for name in names:
            assert name in result.stdout, f"{name} not listed:\n{result.stdout}"


# ── TestCheckHeadOnly ─────────────────────────────────────────────────


class TestCheckHeadOnly:
    """`--check-head` is a dedicated subcommand (PR #52 ADD-1) for
    HEAD-only diagnosis. Does not scan locks, does not clean."""

    def test_check_head_ignores_stale_locks(self, tmp_path):
        _make_fake_repo(
            tmp_path,
            lock_files=("index.lock",),
            lock_age_seconds=60,  # would normally report 🔴 in default mode
        )
        result = _run_script(tmp_path, "--check-head")
        # Must exit 0 (HEAD sane, locks ignored in this mode)
        assert result.returncode == 0
        assert "lock" not in result.stdout.lower()

    def test_check_head_exits_two_on_corrupt(self, tmp_path):
        padded = b"ref: refs/heads/main\n" + b"\x00" * 60
        _make_fake_repo(tmp_path, head_content=padded)
        result = _run_script(tmp_path, "--check-head")
        assert result.returncode == 2
