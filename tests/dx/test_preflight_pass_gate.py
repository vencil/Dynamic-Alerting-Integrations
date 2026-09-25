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
import shutil
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


# ---------------------------------------------------------------------------
# The marker names the PUSHED commit, not HEAD (#1690's axis, same class)
#
# ⛔ Every test above pushes a refspec whose local_sha IS HEAD, so none of them
# can tell the two apart. "Standing on A while pushing B" is ordinary here —
# this repo runs ~18 worktrees — and it broke in BOTH directions.
# ---------------------------------------------------------------------------


def _second_branch(repo: Path, name: str) -> str:
    """Commit one more thing on `name`, then return to main. Returns its sha."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }

    def g(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True, env=env,
        )

    g("checkout", "-q", "-b", name, "main")
    (repo / f"{name.replace('/', '_')}.txt").write_text("work\n")
    g("add", "-A")
    g("commit", "-q", "-m", f"work on {name}")
    sha = g("rev-parse", "HEAD").stdout.strip()
    g("checkout", "-q", "main")
    return sha


def test_a_marker_on_the_pushed_commit_allows_while_standing_elsewhere(
    tmp_path: Path,
):
    """The false-red half: preflight ran on B, the pusher is standing on A.

    Measured before the fix: the marker for B's tip was sitting in .git, the
    gate looked for A's, and rc was 1 — and the banner then told the pusher to
    run preflight, which (from A) writes A's marker and leaves the push
    blocked. There was no instruction in it that reached green.
    """
    head_sha = _init_git(tmp_path)
    feat_sha = _second_branch(tmp_path, "feat/p")
    assert feat_sha != head_sha
    (tmp_path / ".git" / f".preflight-ok.{feat_sha}").touch()

    r = _run_gate(
        tmp_path, _refspec("feat/p", feat_sha),
        path_prepend=_make_fake_gh(tmp_path / "bin", state="OPEN"),
        env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 0, (
        "preflight had passed on the commit being pushed and the gate blocked "
        f"anyway. stderr={r.stderr}"
    )


def test_a_marker_on_head_does_not_vouch_for_a_different_pushed_commit(
    tmp_path: Path,
):
    """The fail-open half, and the reason this is a gate defect not an annoyance.

    Keying to HEAD does not merely block verified pushes — it APPROVES
    unverified ones. Standing on a branch you happened to preflight, pushing a
    different branch by name, and the gate says yes to a commit nothing ever
    checked. Measured on the pre-fix script with this fixture: rc=0.
    """
    head_sha = _init_git(tmp_path)
    feat_sha = _second_branch(tmp_path, "feat/p")
    # Marker for where we STAND. Nothing ever ran against feat/p.
    (tmp_path / ".git" / f".preflight-ok.{head_sha}").touch()

    r = _run_gate(
        tmp_path, _refspec("feat/p", feat_sha),
        path_prepend=_make_fake_gh(tmp_path / "bin", state="OPEN"),
        env_extra={"GIT_PREFLIGHT_STRICT": "1", "TMPDIR": str(tmp_path.parent)},
    )
    assert r.returncode == 1, (
        "an unverified commit was approved because an unrelated commit had a "
        f"marker. stdout={r.stdout} stderr={r.stderr}"
    )
    # ⛔ Anchored to the `Pushing:` line specifically. A bare `feat_sha in
    # stderr` is also satisfied by the `Missing marker:` filename, so it stayed
    # green with the banner printing HEAD on that line — measured.
    assert f"-> {feat_sha}" in r.stderr, (
        "the banner must name the commit that is actually missing a marker, "
        f"not the one the pusher is standing on. stderr={r.stderr}"
    )
    # The recovery instruction must reach the pushed commit, not re-mark the
    # one the pusher stands on.
    head, _ = _follow_the_hint(r.stderr, tmp_path)
    assert head == feat_sha


def test_every_pushed_commit_needs_its_own_marker_not_just_one(
    tmp_path: Path,
):
    """`git push origin a b` publishes two commits, so it needs two markers.

    ⛔ Not "any" — the quantifier is the whole point. Marking one branch and
    riding it for a second unverified one is the same hole as the HEAD case,
    reached through a multi-ref push instead of a checkout.
    """
    _init_git(tmp_path)
    p_sha = _second_branch(tmp_path, "feat/p")
    q_sha = _second_branch(tmp_path, "feat/q")
    (tmp_path / ".git" / f".preflight-ok.{p_sha}").touch()

    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    both = _refspec("feat/p", p_sha) + _refspec("feat/q", q_sha)
    r = _run_gate(tmp_path, both, path_prepend=shim,
                  env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r.returncode == 1, (
        f"one marker vouched for two branches. stderr={r.stderr}"
    )
    assert q_sha in r.stderr, f"the unmarked branch is not named. stderr={r.stderr}"

    # Control: mark the second one too and the same push goes through.
    (tmp_path / ".git" / f".preflight-ok.{q_sha}").touch()
    r2 = _run_gate(tmp_path, both, path_prepend=shim,
                   env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r2.returncode == 0, f"stderr={r2.stderr}"


def test_an_empty_local_sha_falls_back_to_head_rather_than_blocking(
    tmp_path: Path,
):
    """First push of a branch to an empty remote reports no sha at all.

    pre-commit's env channel exports no TO_REF on that path (see
    _prepush_refs.sh, FIELD ORDER), so the row's sha column is EMPTY. No
    marker can ever exist for "", so keying strictly to the pushed sha would
    turn a legitimate first push into a permanent block. HEAD is the best
    answer available there, and it is what this gate has always used.

    ⛔ Driven through the ENV channel, not stdin. An empty MIDDLE field cannot
    be expressed on stdin at all — default-IFS `read` collapses it, which is
    the whole reason that channel exists. A stdin row written to look like
    this one parses as a single field, `remote_ref` comes out empty, and the
    gate drops the row before reaching any of this: measured, the test then
    passed without ever entering the branch it names.
    """
    head_sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{head_sha}").touch()

    r = _run_gate(
        tmp_path, "",
        path_prepend=_make_fake_gh(tmp_path / "bin", state="OPEN"),
        env_extra={
            "GIT_PREFLIGHT_STRICT": "1",
            "PRE_COMMIT": "1",
            "PRE_COMMIT_REMOTE_BRANCH": "refs/heads/newbr",
            # ⛔ PRE_COMMIT_TO_REF deliberately absent — that IS the shape.
        },
    )
    assert r.returncode == 0, (
        "a first push to an empty remote was blocked with no reachable "
        f"recovery. stderr={r.stderr}"
    )


# ---------------------------------------------------------------------------
# Unknown must not mean OK, and one row must not silence the others
#
# ⛔ Every one of these was measured as a live fail-open or a dead-end
# instruction before the fix; each assertion below reds when its own mechanism
# is reverted, and none of the tests above notice.
# ---------------------------------------------------------------------------


def test_a_marker_written_in_another_worktree_is_visible_here(tmp_path: Path):
    """Markers live in the SHARED git dir, not the per-worktree one.

    A marker says "preflight passed on this COMMIT" — not a property of the
    worktree it was run in. Keyed to `--git-dir`, a marker written in one
    worktree was invisible everywhere else, so a commit that had in fact
    passed was blocked. Two independent blind reviews measured the same dead
    end: the banner said to check the branch out, git refused because another
    worktree held it, and running preflight there wrote a marker the pushing
    side still could not see.
    """
    _init_git(tmp_path)
    wt = tmp_path.parent / "sibling-wt"
    assert subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "worktree", "add", "-q", "-b", "sibling",
         str(wt), "main"],
        capture_output=True, text=True,
    ).returncode == 0

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    (wt / "b.txt").write_text("work\n")
    for args in (["add", "-A"], ["commit", "-q", "-m", "sibling work"]):
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(wt), *args], capture_output=True, text=True, env=env,
        ).returncode == 0
    sib_sha = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(wt), "rev-parse", "HEAD"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()

    # Preflight run inside that worktree writes to the SHARED dir.
    common = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(wt), "rev-parse", "--path-format=absolute",
         "--git-common-dir"],
        capture_output=True, text=True, env=env,
    ).stdout.strip()
    assert common, "could not resolve the shared git dir"
    (Path(common) / f".preflight-ok.{sib_sha}").touch()

    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")

    # ⛔ Run the gate FROM the linked worktree. From the main checkout the two
    # spellings resolve to the same `.git`, so that direction cannot tell them
    # apart — measured: reverting to `--git-dir` left a main-checkout version
    # of this test green.
    r = _run_gate(
        wt, _refspec("sibling", sib_sha),
        path_prepend=shim, env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 0, (
        "a commit marked in the shared git dir was blocked from inside a "
        f"linked worktree. stderr={r.stderr}"
    )

    # The user-visible direction: the same marker also satisfies a push made
    # from the main checkout.
    r_main = _run_gate(
        tmp_path, _refspec("sibling", sib_sha),
        path_prepend=shim, env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r_main.returncode == 0, f"stderr={r_main.stderr}"

    # CONTROL: remove it and the gate must block again.
    (Path(common) / f".preflight-ok.{sib_sha}").unlink()
    r2 = _run_gate(
        tmp_path, _refspec("sibling", sib_sha),
        path_prepend=shim, env_extra={"GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r2.returncode == 1, f"control did not fire. stderr={r2.stderr}"


def _follow_the_hint(stderr: str, cwd: Path) -> tuple[str, Path]:
    """Paste the banner's instruction into a shell standing in `cwd`, with
    `make pr-preflight` swapped for a probe; return (HEAD, directory) where the
    probe ran.

    ⛔ The shell must end where it started: a relative refspec is re-read from
    there on the next push.
    """
    r = _paste_the_hint(stderr, cwd, 'printf "\\0probe\\0%s\\0%s\\0" "$(git rev-parse HEAD)" "$(pwd -P)"')
    assert r.returncode == 0, f"the instruction failed: {r.stderr}"
    assert "\0probe\0" in r.stdout, f"the instruction did not reach the probe: {r.stderr}"
    head, landed, _ = r.stdout.split("\0probe\0", 1)[1].split("\0", 2)
    assert Path(r.stdout.split("\0after\0", 1)[1]) == cwd.resolve(), (
        "the instruction moved the shell you push from"
    )
    return head, Path(landed)


def _paste_the_hint(stderr: str, cwd: Path, preflight: str) -> subprocess.CompletedProcess:
    """Run the banner's one instruction line with `make pr-preflight` replaced
    by `preflight`, then report where the shell ended; rc is the line's."""
    lines = [ln.strip() for ln in stderr.splitlines() if "make pr-preflight" in ln]
    assert len(lines) == 1, f"expected one instruction line: {stderr}"
    script = (lines[0].replace("make pr-preflight", preflight)
              + '; r=$?; printf "\\0after\\0%s" "$(pwd -P)"; exit $r')
    return subprocess.run(  # subprocess-timeout: ignore
        ["bash", "-c", script], cwd=cwd, capture_output=True, text=True,
    )


def _held_worktree(tmp_path: Path, name: str, commits: int = 1,
                   parent: Path | None = None) -> tuple[Path, list[str]]:
    """Main repo + a worktree `name` (under `parent`, default a directory next
    to the repo that also holds a plain `sibling`) on branch `held`,
    `commits` commits past main. Returns (wt, shas)."""
    _init_git(tmp_path)
    if parent is None:
        parent = tmp_path.parent / f"wts-{tmp_path.name}"
        (parent / "sibling").mkdir(parents=True)
    wt = parent / name
    assert _git(tmp_path, "worktree", "add", "-q", "-b", "held", str(wt), "main").returncode == 0
    shas = []
    for i in range(commits):
        assert _git(wt, "commit", "-q", "--allow-empty", "-m", f"held {i}").returncode == 0
        shas.append(_git(wt, "rev-parse", "HEAD").stdout.strip())
    return wt, shas


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
    }
    return subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env,
    )


def _blocked(pushing_from: Path, sha: str) -> subprocess.CompletedProcess:
    r = _run_gate(
        pushing_from, _refspec("held", sha),
        path_prepend=_make_fake_gh(pushing_from.parent / f"bin-{pushing_from.name}", state="OPEN"),
        env_extra={"GIT_PREFLIGHT_STRICT": "1", "TMPDIR": str(pushing_from.parent)},
    )
    assert r.returncode == 1, f"stderr={r.stderr}"
    return r


@pytest.mark.parametrize(
    "name",
    ["sibling-wt", "sibling wt", "sibling'wt", "sibling$HOME", "sibling ",
     "sib\\ling", "sibling\nwt"],
    ids=["plain", "space", "quote", "dollar", "trailing-space", "backslash", "newline"],
)
def test_the_hint_lands_in_the_worktree_at_the_pushed_commit(tmp_path: Path, name: str):
    """#1952 — the printed `cd` is executed, not substring-matched.

    A plain directory `sibling` sits next to it: where a path cut at its space
    or newline would land.
    """
    wt, (sha,) = _held_worktree(tmp_path, name)
    head, landed = _follow_the_hint(_blocked(tmp_path, sha).stderr, tmp_path)
    assert (head, landed) == (sha, wt.resolve())


@pytest.mark.parametrize("shape", [
    "pushed-from-another-tree", "pushing-tree-was-moved", "prunable-entry-listed-first",
])
def test_the_hint_enters_the_tree_already_at_the_pushed_commit(tmp_path: Path, shape: str):
    """#1952 — a tree whose HEAD is the pushed commit is where the `cd` goes."""
    wt, (sha,) = _held_worktree(tmp_path, "sibling wt")
    pushing_from = tmp_path
    if shape == "pushed-from-another-tree":
        pushing_from = wt  # `git -C <wt> push` from a shell in the main checkout
    elif shape == "pushing-tree-was-moved":
        # Still a working tree; `git worktree list` shows it, prunable, at its
        # old path.
        moved = wt.parent / "moved"
        wt.rename(moved)
        wt = pushing_from = moved
    else:
        # Worktrees are listed by path: this one comes before `sibling wt`.
        gone = wt.parent / "a-gone"
        assert _git(tmp_path, "worktree", "add", "-q", "--detach", str(gone), "main").returncode == 0
        shutil.rmtree(gone)
    head, landed = _follow_the_hint(_blocked(pushing_from, sha).stderr, tmp_path)
    assert (head, landed) == (sha, wt.resolve())


@pytest.mark.parametrize("shape", [
    "held-at-a-later-commit", "worktree-dir-gone", "locked-and-gone",
    "nested-without-its-git-file",
])
def test_with_no_tree_at_the_pushed_commit_the_hint_leaves_yours_alone(tmp_path: Path, shape: str):
    """#1952 — no tree sits at the pushed commit, so the instruction makes one,
    leaves the tree you push from where it is, and cleans up after itself.
    Followed from outside the repository: `git -C <tree> push` is one way in.
    """
    if shape == "nested-without-its-git-file":
        # Its directory is still there, inside the main checkout: entering it
        # lands in the main checkout's HEAD.
        wt, shas = _held_worktree(tmp_path, "sibling wt", parent=tmp_path / ".wt")
    else:
        wt, shas = _held_worktree(tmp_path, "sibling wt", commits=2)
    sha = shas[-1]
    if shape == "held-at-a-later-commit":
        sha = shas[0]
    elif shape == "worktree-dir-gone":
        shutil.rmtree(wt)
    elif shape == "locked-and-gone":
        assert _git(tmp_path, "worktree", "lock", str(wt)).returncode == 0
        shutil.rmtree(wt)
    else:
        (wt / ".git").unlink()
    before = (_git(tmp_path, "rev-parse", "HEAD").stdout, _git(tmp_path, "symbolic-ref", "HEAD").stdout)

    head, landed = _follow_the_hint(_blocked(tmp_path, sha).stderr, tmp_path.parent)

    assert head == sha
    assert landed.parent == tmp_path.parent.resolve(), "not under the gate's TMPDIR"
    assert (_git(tmp_path, "rev-parse", "HEAD").stdout, _git(tmp_path, "symbolic-ref", "HEAD").stdout) == before
    assert str(landed) not in _git(tmp_path, "worktree", "list", "--porcelain").stdout, (
        "the clean-up line left the throwaway worktree registered"
    )


def test_a_failing_preflight_in_the_throwaway_worktree_fails_the_line_and_cleans_up(tmp_path: Path):
    """#1952 — the line's exit code is preflight's, and its worktree goes either way."""
    wt, shas = _held_worktree(tmp_path, "sibling wt", commits=2)
    r = _paste_the_hint(_blocked(tmp_path, shas[0]).stderr, tmp_path, "false")
    assert r.returncode != 0
    assert _git(tmp_path, "worktree", "list", "--porcelain").stdout.count("worktree ") == 2, (
        "the throwaway worktree outlived a failing preflight"
    )


def test_a_bare_repository_pushing_its_head_gets_the_instruction(tmp_path: Path):
    """#1952 — a bare repository has no toplevel; asking for one must not end
    the gate before it prints anything."""
    _init_git(tmp_path)
    bare = tmp_path.parent / f"bare-{tmp_path.name}.git"
    assert _git(tmp_path, "clone", "-q", "--bare", str(tmp_path), str(bare)).returncode == 0
    sha = _git(bare, "rev-parse", "HEAD").stdout.strip()
    r = _run_gate(
        bare, _refspec("held", sha),
        path_prepend=_make_fake_gh(tmp_path / "bin", state="OPEN"),
        env_extra={"GIT_PREFLIGHT_STRICT": "1", "TMPDIR": str(tmp_path.parent)},
    )
    assert r.returncode == 1, f"stderr={r.stderr}"
    head, landed = _follow_the_hint(r.stderr, bare)
    assert head == sha
    assert str(landed) not in _git(bare, "worktree", "list", "--porcelain").stdout


def test_an_empty_gh_answer_is_unknown_not_no_pr(tmp_path: Path):
    """`gh` exiting 0 while printing nothing must require the marker.

    Measured on the real CLI: `gh pr list --head <b> --state open --json
    number --jq length` prints `0` when there is no PR and `1` when there is —
    it is never empty. Empty therefore means the query produced nothing while
    still exiting 0 (a wrapper, a pager, a stripped exporter), and folding it
    in with `0` made unknown mean OK.
    """
    sha = _init_git(tmp_path)
    bindir = tmp_path / "emptygh"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gh.chmod(0o755)

    r = _run_gate(tmp_path, _refspec("feat/x", sha), path_prepend=bindir)
    assert r.returncode == 1, (
        "an empty answer was read as 'no open PR', so the gate stood down "
        f"without knowing anything. stderr={r.stderr}"
    )

    # CONTROL: a real `0` still means no PR, and still stands down.
    gh.write_text("#!/bin/sh\necho 0\n", encoding="utf-8")
    gh.chmod(0o755)
    r2 = _run_gate(tmp_path, _refspec("feat/x", sha), path_prepend=bindir)
    assert r2.returncode == 0, (
        f"a genuine 'no open PR' must still be allowed through. stderr={r2.stderr}"
    )


def test_git_missing_refuses_instead_of_allowing_silently(tmp_path: Path):
    """No `git` meant exit 0 with ZERO bytes — the #1664 picture exactly.

    "Checked and fine" and "never ran" have to be different pictures. This one
    is worth naming: the gate's own header spends a section on that principle
    while its second executable line did the opposite.
    """
    sha = _init_git(tmp_path)
    (tmp_path / ".git" / f".preflight-ok.{sha}").touch()  # irrelevant; unreadable
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    for tool in ("bash", "sh", "cat", "basename"):
        src = shutil.which(tool)
        if src:
            try:
                os.symlink(src, nogit / tool)
            except OSError:
                shutil.copy2(src, nogit / tool)
    gh = nogit / "gh"
    gh.write_text("#!/bin/sh\necho 1\n", encoding="utf-8")
    gh.chmod(0o755)

    r = subprocess.run(  # subprocess-timeout: ignore
        ["bash", str(_SH_SCRIPT)],
        cwd=tmp_path, input=_refspec("feat/x", sha), capture_output=True,
        text=True, env={"PATH": str(nogit), "GIT_PREFLIGHT_STRICT": "1"},
    )
    assert r.returncode == 1, (
        f"the gate allowed a push it could not judge. stderr={r.stderr}"
    )
    assert (r.stdout + r.stderr).strip(), (
        "it refused silently — zero bytes is indistinguishable from not "
        "having run at all"
    )


def test_a_main_row_does_not_silence_the_other_branches(tmp_path: Path):
    """`git push origin main feat/x` must still judge feat/x.

    protect_main_push owns the verdict on main, so this gate stays quiet about
    that row — but it used to exit on the whole push, which turned "every
    pushed commit needs a marker" into "any row can excuse all of them".
    """
    _init_git(tmp_path)
    feat_sha = _second_branch(tmp_path, "feat/y")
    main_sha = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(tmp_path), "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()

    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    both = _refspec("main", main_sha) + _refspec("feat/y", feat_sha)
    r = _run_gate(tmp_path, both, path_prepend=shim,
                  env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r.returncode == 1, (
        "a main row excused an unverified feature branch in the same push. "
        f"stderr={r.stderr}"
    )
    assert feat_sha in r.stderr, f"the feature row is not named. stderr={r.stderr}"

    # CONTROL: main ALONE is still deferred to protect_main_push, silently.
    r2 = _run_gate(tmp_path, _refspec("main", main_sha), path_prepend=shim,
                   env_extra={"GIT_PREFLIGHT_STRICT": "1"})
    assert r2.returncode == 0, (
        f"a main-only push must stay this gate's business. stderr={r2.stderr}"
    )


def test_tag_pushes_and_deletions_are_allowed_as_the_header_promises(
    tmp_path: Path,
):
    """Two promises the header makes that nothing was checking.

    Both were measured SURVIVED under mutation: removing the `refs/tags/*`
    skip, and removing the deletion skip, left the whole file green. The
    six-line release tag push goes through here, and `gh` is absent in the
    dev container, so a tag push landing in the marker branch blocks a
    release.
    """
    sha = _init_git(tmp_path)
    shim = _make_fake_gh(tmp_path / "bin", state="OPEN")
    strict = {"GIT_PREFLIGHT_STRICT": "1"}

    tag = _run_gate(
        tmp_path, f"refs/tags/v9.9.9 {sha} refs/tags/v9.9.9 {ZERO_SHA}\n",
        path_prepend=shim, env_extra=strict,
    )
    assert tag.returncode == 0, (
        f"a tag push was gated on a branch marker. stderr={tag.stderr}"
    )

    dele = _run_gate(
        tmp_path, f"refs/heads/doomed {ZERO_SHA} refs/heads/doomed {sha}\n",
        path_prepend=shim, env_extra=strict,
    )
    assert dele.returncode == 0, (
        f"deleting a branch was gated on a marker. stderr={dele.stderr}"
    )

    # CONTROL: an ordinary branch row with no marker still blocks, so the two
    # allowances above are not the whole gate standing down.
    ctl = _run_gate(tmp_path, _refspec("feat/x", sha), path_prepend=shim,
                    env_extra=strict)
    assert ctl.returncode == 1, f"control did not fire. stderr={ctl.stderr}"
