"""The test session's git config: the host's, with signing off (#3896).

tests/conftest.py's `_git_config_without_host_signing` points
GIT_CONFIG_GLOBAL at a copy of the host's system + global git config with
commit.gpgsign / tag.gpgsign forced false (tests/_git_isolation.py). These
tests pin that it is active in this process, and that the copy turns a
hostile host config (signing on, a signer that always fails) into one a test
commit and tag go through — while that same hostile config, used directly,
does call the signer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import _git_isolation as gi

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")

SIGNER_MARK = "host signer called (#3896)"


def _git(*args, cwd, env, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False, timeout=60)
    if check:
        assert proc.returncode == 0, (args, proc.stdout, proc.stderr)
    return proc


def _hostile_global(tmp_path: Path) -> Path:
    """A global config that signs every commit and tag with a signer that
    always fails, plus entries whose copy must survive verbatim."""
    signer = tmp_path / "signer.sh"
    signer.write_text(f"#!/bin/sh\necho '{SIGNER_MARK}' >&2\nexit 1\n", encoding="utf-8")
    signer.chmod(0o755)
    included = tmp_path / "included.gitconfig"
    included.write_text('[alias]\n\tfrominclude = "status --short"\n', encoding="utf-8")
    cfg = tmp_path / "hostile.gitconfig"
    cfg.write_text(
        "[user]\n\tname = Test\n\temail = test@example.invalid\n"
        "\tsigningkey = DEADBEEF\n"
        f"[gpg]\n\tformat = openpgp\n\tprogram = {signer}\n"
        "[commit]\n\tgpgsign = true\n[tag]\n\tgpgsign = true\n"
        "[init]\n\tdefaultBranch = main\n"
        # multi-valued, a value that needs quoting, a subsection with dots,
        # a key without "=" and an include: all must come through the copy
        "[remote \"o.r\"]\n\tfetch = +refs/heads/a:refs/x/a\n"
        "\tfetch = +refs/heads/b:refs/x/b\n"
        "[vibe3896]\n\tquoted = \"a \\\"b\\\" \\\\ ; # c\"\n\tbare\n"
        f"[include]\n\tpath = {included}\n",
        encoding="utf-8")
    return cfg


def _hostile_env(tmp_path: Path, global_cfg: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_CONFIG", "GIT_AUTHOR_", "GIT_COMMITTER_"))}
    env.update(GIT_CONFIG_GLOBAL=str(global_cfg), GIT_CONFIG_NOSYSTEM="1",
               GIT_CEILING_DIRECTORIES=str(tmp_path))
    return env


def _commit_and_tag(repo: Path, env: dict) -> "list[subprocess.CompletedProcess]":
    _git("init", "-q", str(repo), cwd=repo.parent, env=env)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git("add", "f.txt", cwd=repo, env=env)
    commit = _git("commit", "-q", "-m", "c", cwd=repo, env=env, check=False)
    tag = _git("tag", "-a", "-m", "t", "t1", cwd=repo, env=env, check=False)
    return [commit, tag]


def test_the_session_points_git_at_a_copy_with_signing_off():
    path = os.environ.get("GIT_CONFIG_GLOBAL")
    assert path and os.path.isfile(path), path
    assert os.path.basename(path) == gi.CONFIG_FILE_NAME
    assert os.environ.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert gi.read_key(path, "commit.gpgsign") == "false"
    assert gi.read_key(path, "tag.gpgsign") == "false"
    assert gi.read_key(path, "maintenance.auto") == "false"


def test_a_commit_through_the_session_environment_needs_no_signer(tmp_path):
    """A test that runs git with the inherited os.environ commits under the
    session's config, whatever the host's says."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CEILING_DIRECTORIES=str(tmp_path))
    for proc in _commit_and_tag(repo, env):
        assert proc.returncode == 0, proc.stderr


def test_the_hostile_config_does_call_the_signer(tmp_path):
    """The counterfactual the next test needs: used directly, this config
    makes both the commit and the tag call the failing signer."""
    env = _hostile_env(tmp_path, _hostile_global(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    commit, _tag = _commit_and_tag(repo, env)
    assert commit.returncode != 0 and SIGNER_MARK in commit.stderr, commit.stderr
    _git("commit", "-q", "--no-gpg-sign", "-m", "c", cwd=repo, env=env)
    tag = _git("tag", "-a", "-m", "t", "t1", cwd=repo, env=env, check=False)
    assert tag.returncode != 0 and SIGNER_MARK in tag.stderr, tag.stderr


def test_the_copy_of_a_hostile_config_commits_and_tags_without_the_signer(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    env = _hostile_env(host, _hostile_global(host))
    out = tmp_path / "copy"
    out.mkdir()
    path = gi.write_config_copy(str(out), env)
    copied = dict(env, GIT_CONFIG_GLOBAL=path)
    repo = tmp_path / "repo"
    repo.mkdir()
    for proc in _commit_and_tag(repo, copied):
        assert proc.returncode == 0, proc.stderr
        assert SIGNER_MARK not in proc.stderr


def test_the_copy_keeps_every_other_entry_in_order(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    env = _hostile_env(host, _hostile_global(host))
    out = tmp_path / "copy"
    out.mkdir()
    path = gi.write_config_copy(str(out), env)

    def get_all(key):
        return _git("config", "--file", path, "--get-all", key, cwd=tmp_path,
                    env=env).stdout.splitlines()

    assert get_all("remote.o.r.fetch") == ["+refs/heads/a:refs/x/a", "+refs/heads/b:refs/x/b"]
    assert get_all("vibe3896.quoted") == ['a "b" \\ ; # c']
    assert _git("config", "--file", path, "--type=bool", "--get", "vibe3896.bare",
                cwd=tmp_path, env=env).stdout.strip() == "true"
    assert get_all("user.signingkey") == ["DEADBEEF"]
    # the include was resolved into the copy, and is not followed again
    assert get_all("alias.frominclude") == ["status --short"]
    assert _git("config", "--file", path, "--get-all", "include.path", cwd=tmp_path,
                env=env, check=False).returncode == 1
    # the host's "true" is still there, before the "false" that wins
    assert get_all("commit.gpgsign") == ["true", "false"]


def test_no_repository_config_is_copied(tmp_path):
    """The listing runs outside any repository: a repo the copy is made in
    (or above) contributes nothing."""
    host = tmp_path / "host"
    host.mkdir()
    env = _hostile_env(tmp_path, _hostile_global(host))
    _git("init", "-q", str(tmp_path), cwd=tmp_path, env=env)
    _git("config", "vibe3896.local", "yes", cwd=tmp_path, env=env)
    out = tmp_path / "copy"
    out.mkdir()
    path = gi.write_config_copy(str(out), env)
    assert gi.read_key(path, "vibe3896.local") is None
    assert gi.read_key(path, "user.name") == "Test"


def test_an_empty_host_config_copies_to_signing_off_alone(tmp_path):
    env = _hostile_env(tmp_path, Path(os.devnull))
    path = gi.write_config_copy(str(tmp_path), env)
    assert Path(path).read_text(encoding="utf-8") == (
        "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
        "[maintenance]\n\tauto = false\n")


def test_a_commit_under_the_session_starts_no_background_maintenance(tmp_path):
    """#4037: without the session's maintenance.auto=false, a commit spawns
    `git maintenance run --auto` (detached), whose worktree-prune (git ≥ 2.50)
    removed a worktree `git worktree add` was still making. Read from
    GIT_TRACE: the counterfactual first, then the session's environment."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid",
               GIT_CEILING_DIRECTORIES=str(tmp_path), GIT_TRACE="1")
    _git("init", "-q", cwd=repo, env=env)

    def spawns(n, *extra):
        (repo / f"f{n}.txt").write_text("x\n", encoding="utf-8")
        _git("add", f"f{n}.txt", cwd=repo, env=env)
        proc = _git(*extra, "commit", "-q", "-m", "c", cwd=repo, env=env)
        return "maintenance run --auto" in proc.stderr

    assert spawns(1, "-c", "maintenance.auto=true"), "the counterfactual: git does start it"
    assert not spawns(2), "the session's config starts no maintenance"


def test_a_config_git_cannot_read_fails_the_copy(tmp_path):
    bad = tmp_path / "bad.gitconfig"
    bad.write_text("[unterminated\n", encoding="utf-8")
    env = _hostile_env(tmp_path, bad)
    out = tmp_path / "copy"
    out.mkdir()
    with pytest.raises(gi.GitConfigCopyError):
        gi.write_config_copy(str(out), env)
