"""Tests for the conditional pre-push marker gate (PR #44 C7).

The gate in `scripts/ops/require_preflight_pass.sh` consults
`gh pr list --head <branch> --state open --json number --jq length` per
pushed branch:

    * STRICT (GIT_PREFLIGHT_STRICT=1) → always require marker (old behavior).
    * gh available + OPEN PR exists    → require marker.
    * gh available + no OPEN PR        → allow (WIP branch, pre-review).
    * gh missing, or the query fails   → require marker (safe fallback).

The query deliberately is not `gh pr view`: that command exits non-zero for
a branch with no PR as well, so its exit code cannot separate "no PR" from
"the query failed" — and the two must land on opposite sides of this gate.

These tests fake `gh` on PATH via a shim shell script so we can control
its output and exit code deterministically, without needing an actual
GitHub remote.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Every test in this module invokes require_preflight_pass.sh via
# `bash <script-path>`. Git Bash on Windows mangles `C:\path\file`
# argument translation so the script can never be found. Linux CI is
# the production target; skip cleanly on Windows instead of letting
# every test fail with a misleading "No such file or directory" error.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash gate-script tests need POSIX path translation; "
           "Git Bash on Windows mangles 'C:\\path\\file' arguments. "
           "Verified to pass on Linux CI runners.",
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SH_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "require_preflight_pass.sh"
ZERO_SHA = "0" * 40


# ---------------------------------------------------------------------------
# Helpers: init ephemeral repo + build a fake `gh` on PATH
# ---------------------------------------------------------------------------


def _init_git(repo: Path) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)  # subprocess-timeout: ignore
    (repo / "a.txt").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)  # subprocess-timeout: ignore
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],  # subprocess-timeout: ignore
                   check=True, env=env)
    return subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()


def _make_fake_gh(dir_: Path, *, state: str | None = "OPEN",
                  exit_code: int = 0) -> Path:
    """Write a shim `gh` executable into `dir_` and return its directory.

    The shim matches our real invocation:
        gh pr list --head <branch> --state open --json number --jq length

    That prints how many OPEN PRs have <branch> as their head, so `state`
    maps onto a count — only "OPEN" survives the server-side `--state open`
    filter:

    - state="OPEN"                 → prints 1 (an open PR exists).
    - state=None/"CLOSED"/"MERGED" → prints 0 (query fine, no open PR).
    - exit_code=1 → the query itself fails (not authenticated, API/network
      error). This is NOT "no PR": with `--json` set, `gh pr list` reports an
      empty result as `[]` on exit 0, so a non-zero exit can only mean the
      query did not run to completion.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    gh = dir_ / "gh"
    script = "#!/bin/sh\n"
    if exit_code != 0:
        script += f"exit {exit_code}\n"
    else:
        open_count = 1 if state == "OPEN" else 0
        script += f'printf "%s\\n" "{open_count}"\n'
    gh.write_text(script)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return dir_


def _run_gate(repo: Path, stdin: str, *,
              path_prepend: Path | None = None,
              env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ}
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}{os.pathsep}{env.get('PATH', '')}"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SH_SCRIPT)],
        cwd=repo, input=stdin, capture_output=True, text=True, env=env,
    )


def _refspec(branch: str, sha: str) -> str:
    return f"refs/heads/{branch} {sha} refs/heads/{branch} {ZERO_SHA}\n"


# ---------------------------------------------------------------------------
# STRICT mode → always require marker (preserves old behavior)
# ---------------------------------------------------------------------------


def test_strict_mode_blocks_without_marker_even_with_open_pr(tmp_path: Path):
    sha = _init_git(tmp_path)
    # gh would report OPEN, but STRICT forces marker requirement anyway.
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
        env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 1
    assert "Push blocked" in r.stderr


def test_strict_mode_blocks_without_marker_even_when_no_pr(tmp_path: Path):
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state=None)
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
        env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 1
    assert "Push blocked" in r.stderr


def test_strict_mode_allows_with_marker_present(tmp_path: Path):
    sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{sha}").touch()
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
        env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


# ---------------------------------------------------------------------------
# Non-STRICT: conditional based on OPEN PR
# ---------------------------------------------------------------------------


def test_no_open_pr_allows_without_marker(tmp_path: Path):
    """WIP branch (no PR yet) → gate lets it through even without marker."""
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state=None)  # gh ok, but empty state
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


def test_gh_query_failure_requires_marker(tmp_path: Path):
    """A failed PR query (not logged in, API/network error) is not "no PR".

    `gh pr list --json` reports an empty result as `[]` on exit 0, so a
    non-zero exit means the gate learned nothing about this branch — and an
    unknown PR state has to land on the conservative side, same as `gh`
    being absent altogether.
    """
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", exit_code=1)
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 1, f"stdout: {r.stdout}"


def test_gh_query_failure_with_marker_still_allows(tmp_path: Path):
    """The fallback only reinstates the marker requirement — it does not
    block a push that already has a passing preflight for this HEAD."""
    sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{sha}").touch()
    shim = _make_fake_gh(tmp_path / "bin", exit_code=1)
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


def test_open_pr_blocks_without_marker(tmp_path: Path):
    """PR is open → CI cost matters → gate enforces marker requirement."""
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 1
    assert "Push blocked" in r.stderr
    assert "make pr-preflight" in r.stderr


def test_open_pr_with_marker_allows(tmp_path: Path):
    sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{sha}").touch()
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


def test_closed_pr_treated_as_no_open_pr_allows(tmp_path: Path):
    """Only state == OPEN activates the gate — CLOSED/MERGED fall through."""
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="CLOSED")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


def test_merged_pr_treated_as_no_open_pr_allows(tmp_path: Path):
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="MERGED")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


# ---------------------------------------------------------------------------
# Fallback: gh unavailable → be conservative (require marker)
# ---------------------------------------------------------------------------


def _make_gh_missing_path(tmp_path: Path) -> Path:
    """Build a stripped PATH dir containing only the commands the gate script
    needs — crucially, no `gh`. Symlinks the handful of tools `require_preflight_pass.sh`
    depends on (bash, git, basename, sh, cat) so the shell can still run.
    """
    shim = tmp_path / "nogh_bin"
    shim.mkdir(parents=True, exist_ok=True)
    for tool in ("bash", "git", "basename", "sh", "cat"):
        src = Path("/usr/bin") / tool
        if src.exists():
            target = shim / tool
            if not target.exists():
                target.symlink_to(src)
    return shim


def test_gh_missing_falls_back_to_require_marker(tmp_path: Path):
    """No `gh` on PATH → gate behaves as before (require marker)."""
    sha = _init_git(tmp_path)
    shim = _make_gh_missing_path(tmp_path)
    env = {"PATH": str(shim), "HOME": os.environ.get("HOME", "/tmp")}
    r = subprocess.run(  # subprocess-timeout: ignore
        ["/usr/bin/bash", str(_SH_SCRIPT)],
        cwd=tmp_path, input=_refspec("feat/x", sha),
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 1
    assert "Push blocked" in r.stderr


def test_gh_missing_with_marker_still_allows(tmp_path: Path):
    sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{sha}").touch()
    shim = _make_gh_missing_path(tmp_path)
    env = {"PATH": str(shim), "HOME": os.environ.get("HOME", "/tmp")}
    r = subprocess.run(  # subprocess-timeout: ignore
        ["/usr/bin/bash", str(_SH_SCRIPT)],
        cwd=tmp_path, input=_refspec("feat/x", sha),
        capture_output=True, text=True, env=env,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"


# ---------------------------------------------------------------------------
# Multi-branch push: OPEN PR on any branch activates the gate
# ---------------------------------------------------------------------------


def test_multi_branch_any_open_pr_activates_gate(tmp_path: Path):
    """If any pushed branch has an OPEN PR, gate requires marker."""
    sha = _init_git(tmp_path)
    # Shim that returns OPEN only for branch `feat/b`, nothing for `feat/a`.
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    gh = shim_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        # Args: pr list --head <branch> --state open --json number --jq length
        'if [ "$4" = "feat/b" ]; then printf "1\\n"; else printf "0\\n"; fi\n'
        "exit 0\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    stdin = (
        _refspec("feat/a", sha)
        + _refspec("feat/b", sha)
    )
    r = _run_gate(tmp_path, stdin, path_prepend=shim_dir)
    assert r.returncode == 1
    assert "Push blocked" in r.stderr


def test_multi_branch_all_no_pr_allows(tmp_path: Path):
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state=None)
    stdin = (
        _refspec("feat/a", sha)
        + _refspec("feat/b", sha)
    )
    r = _run_gate(tmp_path, stdin, path_prepend=shim)
    assert r.returncode == 0, f"stderr: {r.stderr}"


# ---------------------------------------------------------------------------
# Orthogonal behavior preserved (bypass, main, deletes, tag pushes)
# ---------------------------------------------------------------------------


def test_bypass_env_still_overrides_everything(tmp_path: Path):
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    r = _run_gate(
        tmp_path, _refspec("feat/x", sha),
        path_prepend=shim,
        env_extra={"GIT_PREFLIGHT_BYPASS": "1"},
    )
    assert r.returncode == 0
    assert "BYPASSED" in r.stderr


def test_pushing_to_main_still_deferred_to_protect_main(tmp_path: Path):
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    # Pushing local feat/x TO refs/heads/main — our gate stays quiet.
    r = _run_gate(
        tmp_path,
        f"refs/heads/feat/x {sha} refs/heads/main {ZERO_SHA}\n",
        path_prepend=shim,
    )
    assert r.returncode == 0
