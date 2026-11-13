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
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "init", "-q", "-b", "main", str(tmp_path)], check=True
    )
    # The repo's own identity, so a later `git commit` (the tool's) never
    # depends on the machine's global config: CI runners have none, and a
    # missing identity is git's fatal 128, not the refusal a test may want.
    for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
        subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "config", key, value], check=True
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
    log = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "log", "--oneline", "-2"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    assert sha[:7] in log.stdout
    assert "feat: add new.txt" in log.stdout

    show = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "show", f"{sha}:new.txt"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
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
    ls = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "ls-tree", sha, "runme.sh"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    )
    assert ls.stdout.startswith("100755")


def test_cli_show_locks_exit_0_when_clean(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    proc = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "--show-locks"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0
    assert "(no phantom locks)" in proc.stdout


def test_cli_show_locks_lists_locks(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / ".git" / "index.lock").write_text("")
    proc = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "--show-locks"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0
    assert "index.lock" in proc.stdout


def test_cli_requires_message_for_commit(tmp_path: Path, monkeypatch) -> None:
    repo = _init_tmp_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "a.txt").write_text("a\n")
    proc = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "a.txt"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
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
    proc = subprocess.run(  # subprocess-timeout: ignore
        [
            sys.executable,
            str(_SCRIPT),
            "--auto",
            "-m",
            "feat: auto-path commit\n",
            "a.txt",
        ],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
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
    proc = subprocess.run(  # subprocess-timeout: ignore
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
        text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode != 0
    assert "not allowed with" in proc.stderr or "argument" in proc.stderr


def _hook(repo: Path, body: str, mode: int = 0o755) -> Path:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(body, encoding="utf-8")
    hook.chmod(mode)
    return hook


def _auto(repo: Path, env: dict | None = None, *,
          cwd: Path | None = None) -> subprocess.CompletedProcess:
    cwd = cwd or repo
    (cwd / "f.txt").write_text("x\n", encoding="utf-8")
    return subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "--auto", "-m", "test: x", "f.txt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd,
        env=env)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, encoding="ascii", check=True, timeout=30).stdout.strip()


@pytest.mark.parametrize("hook_exit", [1, 2, 3, 127])
def test_any_hook_failure_is_no_commit_never_a_finding(tmp_path: Path, hook_exit: int) -> None:
    """#3634 (owner ruling 2026-11-13): git ends every pre-commit hook failure
    at 1 — a finding and a hook that could not run alike (pre-commit
    flattens its hooks' 2/3/127 and a missing executable to 1 too) — so the
    tool cannot prove "change the content": 2, with the hook's own output
    passed through. Before #3634 it was 1."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    _hook(repo, f"#!/bin/sh\necho 'lint: forbidden content' >&2\nexit {hook_exit}\n")
    proc = _auto(repo)
    assert proc.returncode == 2, proc.stderr
    assert "forbidden content" in proc.stderr
    assert "a hook failed" in proc.stderr
    assert _head(repo) == before


def test_a_passing_hook_commits(tmp_path: Path) -> None:
    """Control: the hook runs and passes — the commit lands, exit 0."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    _hook(repo, "#!/bin/sh\nexit 0\n")
    proc = _auto(repo)
    assert proc.returncode == 0, proc.stderr
    assert _head(repo) != before


@pytest.mark.parametrize("hooks_path", [None, "hk"])
def test_a_run_from_a_subdirectory_finds_the_hook(tmp_path: Path, hooks_path: str | None) -> None:
    """`git rev-parse --git-path` answers relative to the current directory:
    a run from a subdirectory must find the same hook, default or a relative
    core.hooksPath, and commit — not refuse a correct install."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    if hooks_path:
        (repo / hooks_path).mkdir()
        hook = repo / hooks_path / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o755)
        subprocess.run(["git", "config", "core.hooksPath", hooks_path], cwd=repo,
                       check=True, timeout=30)
    else:
        _hook(repo, "#!/bin/sh\nexit 0\n")
    (repo / "sub").mkdir()
    proc = _auto(repo, cwd=repo / "sub")
    assert proc.returncode == 0, proc.stderr
    assert _head(repo) != before


def test_nothing_to_commit_says_so(tmp_path: Path) -> None:
    """git says "nothing to commit" on stdout: the error carries it."""
    repo = _init_tmp_repo(tmp_path)
    _hook(repo, "#!/bin/sh\nexit 0\n")
    assert _auto(repo).returncode == 0
    proc = _auto(repo)  # the same content again
    assert proc.returncode == 2, proc.stderr
    assert "nothing to commit" in proc.stderr.split("`git commit` failed")[0], proc.stderr


@pytest.mark.parametrize("state", ["absent", "not-executable", "hooks-path-elsewhere"])
def test_no_runnable_pre_commit_hook_is_no_commit(tmp_path: Path, state: str) -> None:
    """The normal path commits only when git would run the checks: no hook,
    a hook git would skip (not executable), or core.hooksPath pointing away
    from the installed one is 2 and nothing committed — it used to commit
    unchecked with exit 0 (#3634)."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    if state == "not-executable":
        _hook(repo, "#!/bin/sh\nexit 0\n", mode=0o644)
    if state == "hooks-path-elsewhere":
        _hook(repo, "#!/bin/sh\nexit 0\n")
        (repo / "empty-hooks").mkdir()
        subprocess.run(["git", "config", "core.hooksPath", "empty-hooks"], cwd=repo,
                       check=True, timeout=30)
    proc = _auto(repo)
    assert proc.returncode == 2, proc.stderr
    assert "no executable pre-commit hook" in proc.stderr
    assert _head(repo) == before


def test_a_pre_commit_shim_that_cannot_start_pre_commit_is_no_commit(tmp_path: Path) -> None:
    """pre-commit's own shim, but its INSTALL_PYTHON is gone and no
    pre-commit is on PATH: every commit would fail without a check run."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    _hook(repo, "#!/usr/bin/env bash\n# File generated by pre-commit\n"
                f"INSTALL_PYTHON={tmp_path / 'gone' / 'python'}\nexit 1\n")
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if not (Path(p) / "pre-commit").exists())
    proc = _auto(repo, env={**os.environ, "PATH": path})
    assert proc.returncode == 2, proc.stderr
    assert "cannot start pre-commit" in proc.stderr
    assert _head(repo) == before


def test_a_shim_python_that_cannot_be_executed_is_a_named_2(tmp_path: Path) -> None:
    """INSTALL_PYTHON names a directory (os.access says executable): the
    check names it instead of crashing on PermissionError."""
    repo = _init_tmp_repo(tmp_path)
    before = _head(repo)
    (tmp_path / "pydir").mkdir()
    _hook(repo, "#!/usr/bin/env bash\n# File generated by pre-commit\n"
                f"INSTALL_PYTHON={tmp_path / 'pydir'}\nexit 1\n")
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if not (Path(p) / "pre-commit").exists())
    proc = _auto(repo, env={**os.environ, "PATH": path})
    assert proc.returncode == 2, proc.stderr
    assert "cannot start pre-commit" in proc.stderr and "Traceback" not in proc.stderr
    assert _head(repo) == before


def test_no_repo_is_a_caller_error(tmp_path: Path) -> None:
    """#3577: not being in a repository is the caller's (2)."""

    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    proc = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "--show-locks"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=outside,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path)},
    )
    assert proc.returncode == 2, proc.stderr


def test_a_git_fatal_error_on_commit_is_a_caller_error(tmp_path: Path) -> None:
    """git ends a fatal commit error (here: no author identity) at 128, not
    the 1 of a refusal: the commit could not be made, which is 2 — not a
    finding about the content (#3577, #3611 review)."""
    repo = _init_tmp_repo(tmp_path)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    home = tmp_path / "home"
    home.mkdir()
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home), GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, EMAIL="")
    subprocess.run(["git", "config", "--unset", "user.email"], cwd=repo, check=False, timeout=30)
    subprocess.run(["git", "config", "--unset", "user.name"], cwd=repo, check=False, timeout=30)
    subprocess.run(["git", "config", "user.useConfigOnly", "true"], cwd=repo, check=True,
                   timeout=30)
    _hook(repo, "#!/bin/sh\nexit 0\n")  # a hook git runs: the 128 is git's own
    proc = subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, str(_SCRIPT), "--auto", "-m", "test: x", "f.txt"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=repo, env=env,
    )
    assert proc.returncode == 2, proc.stderr
