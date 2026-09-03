"""Tests for pr_preflight.py marker helpers + require_preflight_pass.sh gate.

Plan C (v2.8.0 token-economy): the marker lives at
`.git/.preflight-ok.<HEAD-sha>` and is the contract between
`make pr-preflight` and the pre-push gate.

We test the Python marker helpers in isolation (tmp_path ephemeral repos)
and the bash gate script via subprocess with synthetic stdin + env.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "dx" / "pr_preflight.py"
_SH_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "require_preflight_pass.sh"

# TestGateScript invokes the require_preflight_pass.sh bash script as a
# subprocess. Git Bash on Windows mangles `C:\path\file` argument
# translation (similar to verify_release.sh), so the gate-script tests
# can't run on Windows. The Python-only tests in this module
# (TestWriteMarker / TestClearMarkers / etc.) DO run cross-platform.
_BASH_SCRIPT_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash gate-script tests need POSIX path translation; "
           "Git Bash on Windows mangles 'C:\\path\\file' arguments. "
           "Verified to pass on Linux CI runners.",
)

ZERO_SHA = "0" * 40


def _load():
    spec = importlib.util.spec_from_file_location("pr_preflight", _PY_SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _init_git(repo: Path) -> str:
    """Init a git repo at `repo` with one commit. Returns HEAD sha."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)  # subprocess-timeout: ignore
    (repo / "a.txt").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)  # subprocess-timeout: ignore
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],  # subprocess-timeout: ignore
                   check=True, env=env)
    sha = subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    return sha


class TestPrepushHookInstalled:
    """`pr_preflight._prepush_hook_installed` — the #1664 install-path check.

    ⛔ The middle case is the whole reason it exists: `pre-commit install` —
    what the repo's own install line told people to run — leaves the pre-push
    hook uninstalled. Measured on a fresh clone following that instruction: no
    `.git/hooks/pre-push`, and `git push` straight to main succeeded behind a
    full screen of green pre-commit-stage hooks.

    The second test is the negative control that keeps the probe from decaying
    into "some file is there": a hand-written pre-push hook is not pre-commit's,
    so the three `stages: [pre-push]` guards are still not wired.
    """

    @staticmethod
    def _repo(tmp_path):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],  # subprocess-timeout: ignore
                       check=True, env=env)
        (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "repos: []\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"],  # subprocess-timeout: ignore
                       check=True, env=env)
        subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "-c", "core.hooksPath=/dev/null",
             "commit", "-q", "-m", "init"], check=True, env=env)

    @staticmethod
    def _install(tmp_path, *args):
        return subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-X", "utf8", "-m", "pre_commit", "install", *args],
            cwd=tmp_path, capture_output=True, text=True,
        ).returncode

    def test_absent_then_precommit_only_then_prepush(self, tmp_path, monkeypatch):
        # ⛔ Not a bare `importorskip`. This is the only test that separates
        # `pre-commit install` from `pre-commit install --hook-type pre-push`,
        # so if a CI job loses pre-commit from its `pip install` the assertions
        # below do not fail — they vanish, and the job reports green. That is
        # the #1664 shape one level up. Same flag and same fail-closed shape as
        # tests/ops/test_prepush_hook_wiring.py (`VIBE_REQUIRE_PRE_COMMIT`),
        # which is the sibling this file was measured against.
        if os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1":
            assert importlib.util.find_spec("pre_commit") is not None, (
                "VIBE_REQUIRE_PRE_COMMIT=1 but `pre_commit` is not importable — "
                "the pre-push install probe would have skipped silently. It is "
                "installed by this job's pip install step."
            )
        else:
            pytest.importorskip("pre_commit")
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        assert mod._prepush_hook_installed() is False, "no hooks at all"

        assert self._install(tmp_path) == 0
        assert mod._prepush_hook_installed() is False, (
            "`pre-commit install` on its own installs the pre-commit hook only "
            "— reporting that as installed is the #1664 defect one level up"
        )

        assert self._install(tmp_path, "--hook-type", "pre-push") == 0
        assert mod._prepush_hook_installed() is True

    def test_a_hand_written_prepush_hook_does_not_count(self, tmp_path, monkeypatch):
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        assert mod._prepush_hook_installed() is False


class TestMarkerPython:
    def test_write_marker_creates_file(self, tmp_path, monkeypatch):
        mod = _load()
        sha = _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        p = mod.write_marker(tmp_path)
        assert p is not None
        assert p.exists()
        assert p.name == f".preflight-ok.{sha}"
        assert p.parent.name == ".git"

    def test_write_marker_is_idempotent(self, tmp_path, monkeypatch):
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        p1 = mod.write_marker(tmp_path)
        p2 = mod.write_marker(tmp_path)
        assert p1 == p2
        assert p1.exists()

    def test_clear_markers_removes_all_preflight_files(self, tmp_path, monkeypatch):
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        git_dir = tmp_path / ".git"
        # Plant several stale markers.
        (git_dir / ".preflight-ok.aaa").touch()
        (git_dir / ".preflight-ok.bbb").touch()
        (git_dir / ".preflight-ok.ccc").touch()
        # Unrelated file must survive.
        (git_dir / "config").touch(exist_ok=True)
        n = mod.clear_markers(tmp_path)
        assert n == 3
        assert not list(git_dir.glob(".preflight-ok.*"))
        assert (git_dir / "config").exists()

    def test_clear_on_empty(self, tmp_path, monkeypatch):
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert mod.clear_markers(tmp_path) == 0

    def test_marker_path_uses_head_sha(self, tmp_path, monkeypatch):
        mod = _load()
        sha = _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        p = mod.marker_path(tmp_path, sha)
        assert p.name.endswith(sha)
        assert mod.MARKER_PREFIX in p.name


@_BASH_SCRIPT_SKIP
class TestGateScript:
    """End-to-end behavioural tests of require_preflight_pass.sh."""

    def _run_gate(self, repo: Path, stdin: str, env_extra: dict | None = None):
        env = {**os.environ}
        if env_extra:
            env.update(env_extra)
        return subprocess.run(  # subprocess-timeout: ignore
            ["bash", str(_SH_SCRIPT)],
            cwd=repo, input=stdin, capture_output=True, text=True, env=env,
        )

    def test_bypass_env_always_allows(self, tmp_path):
        _init_git(tmp_path)
        r = self._run_gate(
            tmp_path, "abc 123 refs/heads/feat/x def\n",
            env_extra={"GIT_PREFLIGHT_BYPASS": "1"},
        )
        assert r.returncode == 0
        assert "BYPASSED" in r.stderr

    def test_missing_marker_blocks(self, tmp_path):
        sha = _init_git(tmp_path)
        # STRICT forces the "always require marker" contract this test exists
        # for. Without STRICT, PR #44 C7's conditional gate may let WIP
        # branches through based on gh pr view state.
        r = self._run_gate(
            tmp_path,
            f"refs/heads/feat/x {sha} refs/heads/feat/x 0000000000000000000000000000000000000000\n",
            env_extra={"GIT_PREFLIGHT_STRICT": "1"},
        )
        assert r.returncode == 1
        assert "Push blocked" in r.stderr
        assert "make pr-preflight" in r.stderr

    def test_marker_present_allows(self, tmp_path):
        sha = _init_git(tmp_path)
        (tmp_path / ".git" / f".preflight-ok.{sha}").touch()
        r = self._run_gate(
            tmp_path,
            f"refs/heads/feat/x {sha} refs/heads/feat/x 0000000000000000000000000000000000000000\n",
        )
        assert r.returncode == 0, f"stderr: {r.stderr}"

    def test_pushing_to_main_allowed_here(self, tmp_path):
        """protect_main_push owns blocking main — our gate stays quiet."""
        sha = _init_git(tmp_path)
        # No marker, pushing to main — gate should allow (other hook blocks).
        r = self._run_gate(
            tmp_path,
            f"refs/heads/feat/x {sha} refs/heads/main 0000000000000000000000000000000000000000\n",
        )
        assert r.returncode == 0

    def test_delete_ref_allowed(self, tmp_path):
        """Pushing a delete (local sha = zeros) must not be blocked."""
        _init_git(tmp_path)
        r = self._run_gate(
            tmp_path,
            f"(delete) {ZERO_SHA} refs/heads/feat/x 0123456789abcdef0123456789abcdef01234567\n",
        )
        assert r.returncode == 0

    def test_empty_stdin_allowed(self, tmp_path):
        _init_git(tmp_path)
        r = self._run_gate(tmp_path, "")
        assert r.returncode == 0

    def test_marker_for_different_sha_does_not_allow(self, tmp_path):
        sha = _init_git(tmp_path)
        # Stale marker for a DIFFERENT sha — must not authorize push of `sha`.
        (tmp_path / ".git" / ".preflight-ok.deadbeef0000000000000000000000000000").touch()
        r = self._run_gate(
            tmp_path,
            f"refs/heads/feat/x {sha} refs/heads/feat/x 0000000000000000000000000000000000000000\n",
            env_extra={"GIT_PREFLIGHT_STRICT": "1"},
        )
        assert r.returncode == 1
        assert "Push blocked" in r.stderr
