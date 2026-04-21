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
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "a.txt").write_text("hi")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    return sha


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


class TestGateScript:
    """End-to-end behavioural tests of require_preflight_pass.sh."""

    def _run_gate(self, repo: Path, stdin: str, env_extra: dict | None = None):
        env = {**os.environ}
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
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
