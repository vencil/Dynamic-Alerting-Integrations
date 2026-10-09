"""Tests for pr_preflight.py marker helpers + require_preflight_pass.sh gate.

Plan C (v2.8.0 token-economy): the marker lives at `.git/.preflight-ok.<sha>`
and is the contract between `make pr-preflight` and the pre-push gate.
⛔ The two sides key on different commits by design: the writer marks HEAD
(what it just checked), the gate reads the commits being PUSHED (#1690's
axis). `test_preflight_pass_gate.py` owns that half.

We test the Python marker helpers in isolation (tmp_path ephemeral repos)
and the bash gate script via subprocess with synthetic stdin + env.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _platform_fs import symlink_or_skip  # noqa: E402
from _preflight_checks import stub_checks

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PY_SCRIPT = _REPO_ROOT / "scripts" / "tools" / "dx" / "pr_preflight.py"
_SH_SCRIPT = _REPO_ROOT / "scripts" / "ops" / "require_preflight_pass.sh"
# Resolved on PATH, not passed bare (#2560, same as #2328): on Windows
# CreateProcess searches System32 before PATH, so a bare "bash" is WSL's.
_BASH = shutil.which("bash") or "bash"

# TestGateScript invokes the require_preflight_pass.sh bash script as a
# subprocess. Git Bash on Windows mangles `C:\path\file` argument
# translation (similar to verify_release.sh), so the gate-script tests
# can't run on Windows.
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
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    ).stdout.strip()
    return sha


class TestPrepushWiring:
    """`pr_preflight._prepush_guards_wired` — are the guards on the push path?

    ⛔ This class used to assert that `pre-commit install --hook-type pre-push`
    counts as installed. #1689 made that FALSE and this is where the flip is
    recorded: a hook run by pre-commit gets an empty stdin, and since #2688 the
    guards read nothing else, so a guard run that way sees nothing being pushed
    and prints Passed. The old assertion was a test holding a defect in place.

    There is ONE wired state: the shim, byte for byte what the installer
    writes, executable, at .git/hooks/pre-push. Recognising it by its header
    alone reported a shim with an `exit 0` inserted, a truncated one, or another
    hook type's pre-commit template as wired while a direct push to main went
    through (#2669, #2701). pre-commit's pre-push template is never wired: the
    installer replaces it (why: the installer's header).
    """

    _COPY = (
        "_prepush_refs.sh", "protect_main_push.sh", "require_preflight_pass.sh",
        "pre_push_mkdocs_strict.sh", "prepush_dispatch.sh",
        "install_prepush_hook.sh",
    )

    @classmethod
    def _repo(cls, tmp_path):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
        subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)],  # subprocess-timeout: ignore
                       check=True, env=env)
        # `_install_guards` runs the installer by relative path from the temp
        # repo, so the repo carries a copy of the production scripts, re-read
        # from disk on every run.
        ops = tmp_path / "scripts" / "ops"
        ops.mkdir(parents=True)
        for name in cls._COPY:
            shutil.copy2(_REPO_ROOT / "scripts" / "ops" / name, ops / name)
        (tmp_path / "a.txt").write_text("hi", encoding="utf-8")
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "repos: []\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"],  # subprocess-timeout: ignore
                       check=True, env=env)
        subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "-c", "core.hooksPath=/dev/null",
             "commit", "-q", "-m", "init"], check=True, env=env)

    @staticmethod
    def _precommit_install(tmp_path, *args):
        return subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-X", "utf8", "-m", "pre_commit", "install", *args],
            cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).returncode

    @staticmethod
    def _install_guards(tmp_path):
        bash = shutil.which("bash")
        assert bash, "no bash on PATH"
        return subprocess.run(  # subprocess-timeout: ignore
            [bash, "scripts/ops/install_prepush_hook.sh"],
            cwd=tmp_path, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def _require_pre_commit(self):
        # ⛔ Not a bare `importorskip`. Without pre-commit these assertions do
        # not fail — they vanish, and the job reports green, which is the #1664
        # shape one level up. Same flag as tests/ops/test_prepush_hook_wiring.py.
        if os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1":
            assert importlib.util.find_spec("pre_commit") is not None, (
                "VIBE_REQUIRE_PRE_COMMIT=1 but `pre_commit` is not importable — "
                "the pre-push wiring probe would have skipped silently. It is "
                "installed by this job's pip install step."
            )
        else:
            pytest.importorskip("pre_commit")

    def test_neither_install_command_alone_is_enough(self, tmp_path, monkeypatch):
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        wired, why = mod._prepush_guards_wired()
        assert wired is False, "no hooks at all"
        assert "不存在" in why and "不是一般檔案" not in why, why

        assert self._precommit_install(tmp_path) == 0
        wired, _ = mod._prepush_guards_wired()
        assert wired is False, (
            "`pre-commit install` on its own installs the pre-commit hook only"
        )

        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is False, (
            "pre-commit's own pre-push hook is NOT the wiring any more (#1689): "
            f"a guard it runs sees nothing being pushed. why={why!r}"
        )

    @pytest.mark.parametrize("steps", [
        ("installer", "pre-commit"),
        ("installer", "pre-commit", "pre-commit -f"),
    ])
    def test_pre_commit_taking_the_slot_back_is_caught_and_undone(
        self, tmp_path, monkeypatch, steps
    ):
        """A later `pre-commit install --hook-type pre-push` moves the shim to
        pre-push.legacy; with -f it deletes it, saying nothing (rc=0). Either
        way pre-push is pre-commit's template, which is not wired, and the
        remedy the message gives must bring it back."""
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        hooks = tmp_path / ".git" / "hooks"

        assert self._install_guards(tmp_path).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"CONTROL: the installer alone must be wired: {why!r}"
        for step in steps[1:]:
            force = ["-f"] if step.endswith("-f") else []
            assert self._precommit_install(tmp_path, *force, "--hook-type", "pre-push") == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is False, why
        assert "重跑 install_prepush_hook.sh" in why, why

        r = self._install_guards(tmp_path)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"following the message's remedy did not fix it: {why!r}"
        assert not (hooks / "pre-push.legacy").exists(), "the old shim was left behind"
        assert not (hooks / "pre-push.chained").exists(), (
            f"pre-push.chained was created:\n{r.stdout}")

    @pytest.mark.parametrize("form", ["linux", "windows"])
    def test_the_installer_replaces_pre_commits_template(
        self, tmp_path, monkeypatch, form
    ):
        """pre-commit first, then the installer: the template is replaced by
        the shim, and running the installer again changes nothing.

        Windows: pre-commit puts `#!/bin/sh` above its template and writes
        the file CRLF; the installer must still know it for pre-commit's
        (#2617), or it would refuse it as someone's hook."""
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        # Start from the LF template on every host: on Windows pre-commit has
        # already written the `#!/bin/sh` + CRLF form, and converting that a
        # second time gives a file (`\r\r\n`, two `#!/bin/sh`) no host produces.
        lf = hook.read_bytes().replace(b"\r\n", b"\n").removeprefix(b"#!/bin/sh\n")
        assert lf.startswith(b"#!/usr/bin/env "), lf[:60]
        if form == "windows":
            hook.write_bytes(b"#!/bin/sh\r\n" + lf.replace(b"\n", b"\r\n"))
        else:
            hook.write_bytes(lf)

        for _ in range(2):
            r = self._install_guards(tmp_path)
            assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert hook.read_bytes() == mod._shim_body().encode(), (
            f"pre-push is not the shim:\n{r.stdout}")
        assert not (tmp_path / ".git" / "hooks" / "pre-push.chained").exists(), (
            f"pre-push.chained was created:\n{r.stdout}")
        wired, why = mod._prepush_guards_wired()
        assert wired is True, why

    def test_a_hand_written_prepush_hook_does_not_count(self, tmp_path, monkeypatch):
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        wired, why = mod._prepush_guards_wired()
        assert wired is False
        # The message names the remedy, not what the installer will do with the
        # hook: that depends on state only the installer judges (#2697).
        assert "與安裝器產生的守衛 shim 不同" in why, why
        assert "重跑 install_prepush_hook.sh" in why, why
        # The installer refuses someone's hook and says to fold it elsewhere or
        # delete it (#2746); following that output is the rest of the remedy.
        r = self._install_guards(tmp_path)
        assert r.returncode == 1 and "delete it, then re-run" in r.stderr, r.stderr
        hook.unlink()
        assert self._install_guards(tmp_path).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"following the message's remedy did not fix it: {why!r}"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows has no executable bit; os.access(X_OK) is True for any "
               "existing file, so this property cannot be measured here. It is "
               "measured on Linux, which is what CI and the dev container run.",
    )
    def test_a_shim_without_the_executable_bit_is_not_wired(self, tmp_path, monkeypatch):
        """⛔ git IGNORES a non-executable hook and says so only in a `hint:`
        line that `advice.ignoredHook=false` turns off.

        Measured: with the bit cleared, a direct push to main succeeded and the
        guard banner never appeared, while a content-only probe still reported
        "wired". That is a false green in the one judgement everything else
        relies on, so the bit is part of the judgement.
        """
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        hook = tmp_path / ".git" / "hooks" / "pre-push"

        wired, _ = mod._prepush_guards_wired()
        assert wired is True, "CONTROL: it must be wired before we clear the bit"

        hook.chmod(hook.stat().st_mode & ~0o111)
        wired, why = mod._prepush_guards_wired()
        assert wired is False, "a non-executable shim was reported as wired"
        assert "執行位元" in why, why

        assert self._install_guards(tmp_path).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"following the message's remedy did not fix it: {why!r}"

    @pytest.mark.parametrize("shape", ["directory", "dangling-symlink"])
    def test_a_pre_push_that_is_not_a_regular_file_is_not_called_missing(
        self, tmp_path, monkeypatch, shape
    ):
        """#2697: `is_file()` is False for a directory and for a symlink to
        nothing, and both used to be reported as "does not exist — never
        installed". The installer refuses both (#2702), so the message says what
        is there and to move it aside first; the second half follows that
        remedy once."""
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        if shape == "directory":
            hook.mkdir()
        else:
            symlink_or_skip(tmp_path / "no-such-hook", hook)

        wired, why = mod._prepush_guards_wired()
        assert wired is False, why
        assert "不是一般檔案" in why, why

        moved = tmp_path / "moved-aside"
        hook.rename(moved)
        assert self._install_guards(tmp_path).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"following the message's remedy did not fix it: {why!r}"

    def test_a_symlink_to_the_shim_is_wired(self, tmp_path, monkeypatch):
        """`is_file()` follows the link, so a pre-push that is a symlink to an
        executable copy of the shim is wired: git runs it and the guards fire.
        Only a link to nothing (above) is "not a regular file"."""
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        target = tmp_path / "shim-elsewhere"
        hook.rename(target)
        symlink_or_skip(target, hook)

        wired, why = mod._prepush_guards_wired()
        assert wired is True, why

    def test_an_unreadable_pre_push_is_not_wired(self, tmp_path, monkeypatch):
        """A read error is reported as such and never as wired. Injected at
        `read_bytes` for the hook alone: a mode bit cannot make root (CI's dev
        container, this repo's cloud sessions) fail to read a file."""
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        hook = (tmp_path / ".git" / "hooks" / "pre-push").resolve()
        real = Path.read_bytes

        def read_bytes(self):
            if self.resolve() == hook:
                raise PermissionError(13, "Permission denied", str(self))
            return real(self)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
        wired, why = mod._prepush_guards_wired()
        assert wired is False, why
        assert "讀不到" in why and "install_prepush_hook.sh" in why, why

    @staticmethod
    def _insert_exit_0(hook, tmp_path):
        lines = hook.read_text(encoding="utf-8").split("\n")
        lines.insert(3, "exit 0")
        hook.write_text("\n".join(lines), encoding="utf-8", newline="")

    @staticmethod
    def _truncate(hook, tmp_path):
        hook.write_text("".join(hook.read_text(encoding="utf-8").splitlines(True)[:5]),
                        encoding="utf-8", newline="")

    @staticmethod
    def _leading_blank_line(hook, tmp_path):
        hook.write_bytes(b"\n" + hook.read_bytes())

    @staticmethod
    def _older_version(hook, tmp_path):
        # Every line is still one the shim has; one is gone, as when the
        # installer's SHIM_BODY changed after this copy was written.
        lines = hook.read_text(encoding="utf-8").splitlines(True)
        drop = next(i for i, x in enumerate(lines) if "Work tree looked in" in x)
        hook.write_text("".join(lines[:drop] + lines[drop + 1:]), encoding="utf-8", newline="")

    @staticmethod
    def _another_hook_types_template(hook, tmp_path):
        # The shim sits in pre-push.legacy, and that template never calls it.
        for hook_type in ("pre-push", "pre-rebase"):
            assert TestPrepushWiring._precommit_install(
                tmp_path, "--hook-type", hook_type) == 0
        hook.write_bytes((hook.parent / "pre-rebase").read_bytes())

    @pytest.mark.parametrize("change", [
        "_insert_exit_0", "_truncate", "_leading_blank_line", "_older_version",
        "_another_hook_types_template"])
    def test_a_pre_push_hook_that_is_not_the_installers_shim_is_not_wired(
        self, tmp_path, monkeypatch, change
    ):
        """#2669, #2701: each of these keeps the line the old judgement went
        by, and with each a direct push to main went through while preflight
        said wired. The bit stays set, so only the content can tell."""
        if change == "_another_hook_types_template":
            self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"CONTROL: it must be wired before the change: {why!r}"

        hook = tmp_path / ".git" / "hooks" / "pre-push"
        getattr(self, change)(hook, tmp_path)
        assert os.access(hook, os.X_OK)
        wired, why = mod._prepush_guards_wired()
        assert wired is False, why

        r = self._install_guards(tmp_path)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"following the message's remedy did not fix it: {why!r}"

    @pytest.mark.parametrize("scope", ["local", "global", "local-behind-GIT_CONFIG"])
    @pytest.mark.parametrize("hooks_path", [
        "shared", "/dev/null", "hooks-dir", "", ".git/hooks", "own-absolute"])
    def test_a_set_hooks_path_is_unmeasurable(
        self, tmp_path, monkeypatch, hooks_path, scope
    ):
        """#2696: core.hooksPath can point git at a directory many repositories
        share, where a shim refuses every push of every one of them, and ""
        makes git run no hook at all. While it is set, whatever its value,
        neither the judgement nor the installer goes there, and the message
        names core.hooksPath; unsetting it brings the guards back."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        monkeypatch.chdir(repo)
        target = {"shared": str(tmp_path / "shared"),
                  "own-absolute": str(repo / ".git" / "hooks")}.get(hooks_path, hooks_path)
        before = sorted(p.name for p in (repo / ".git" / "hooks").iterdir())
        # global: the #2696 case, a value every repository on the machine reads.
        # GIT_CONFIG only redirects `git config` itself; git still runs hooks by
        # the local value, so the judgement must not be fooled by it.
        where = ["--global"] if scope == "global" else ["--local"]
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "global.gitconfig"))
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "config", *where, "core.hooksPath", target], cwd=repo).returncode == 0
        if scope == "local-behind-GIT_CONFIG":
            monkeypatch.setenv("GIT_CONFIG", os.devnull)

        wired, why = mod._prepush_guards_wired()
        assert wired is None and "core.hooksPath" in why, why
        r = self._install_guards(repo)
        assert r.returncode == 1 and "core.hooksPath" in r.stderr, r.stderr
        for place in (tmp_path / "shared", repo / "hooks-dir"):
            assert not place.exists(), f"the installer wrote into {place}"
        assert sorted(p.name for p in (repo / ".git" / "hooks").iterdir()) == before

        monkeypatch.delenv("GIT_CONFIG", raising=False)
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "config", *where, "--unset", "core.hooksPath"], cwd=repo).returncode == 0
        assert self._install_guards(repo).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"unsetting core.hooksPath did not bring the guards back: {why!r}"

    @pytest.mark.parametrize("where", ["in-place", "moved", "GIT_DIR"])
    @pytest.mark.parametrize("how", ["worktree-config", "includeIf-gitdir"])
    def test_a_hooks_path_set_for_one_worktree_only_is_unmeasurable(
        self, tmp_path, monkeypatch, how, where
    ):
        """#2772: core.hooksPath can reach a single worktree, and a push from
        there skips .git/hooks while the main checkout reads nothing set. The
        judgement from the main checkout is None and names that worktree, also
        once the worktree was moved without `git worktree repair` (it still
        pushes) and with GIT_DIR exported to the main checkout."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        assert self._install_guards(repo).returncode == 0
        gone = tmp_path / "gone"
        for wt in (tmp_path / "only-this-tree", gone):
            subprocess.run(  # subprocess-timeout: ignore
                ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)],
                check=True)
        shutil.rmtree(gone)
        empty = tmp_path / "empty"
        empty.mkdir()
        gcfg = tmp_path / "global.gitconfig"
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gcfg))
        monkeypatch.chdir(repo)
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"CONTROL: wired before the setting, gone tree included: {why!r}"

        if how == "worktree-config":
            subprocess.run(["git", "config", "extensions.worktreeConfig", "true"],  # subprocess-timeout: ignore
                           check=True)
            subprocess.run(  # subprocess-timeout: ignore
                ["git", "-C", str(tmp_path / "only-this-tree"), "config", "--worktree",
                 "core.hooksPath", str(empty)], check=True)
        else:
            inc = tmp_path / "inc.gitconfig"
            inc.write_text(f"[core]\n\thooksPath = {empty.as_posix()}\n", encoding="utf-8")
            gcfg.write_text(
                f'[includeIf "gitdir:{(repo / ".git" / "worktrees" / "only-this-tree").as_posix()}"]\n'
                f"\tpath = {inc.as_posix()}\n", encoding="utf-8")
        bare = tmp_path / "bare.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)  # subprocess-timeout: ignore
        if where == "moved":
            src = tmp_path / "moved-away"
            shutil.move(str(tmp_path / "only-this-tree"), str(src))
        else:
            src = tmp_path / "only-this-tree"
        push = subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(src), "push", "-q", str(bare), "HEAD:refs/heads/main"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            env={**os.environ, "GIT_PREFLIGHT_BYPASS": "1", "MKDOCS_STRICT_BYPASS": "1"})
        assert push.returncode == 0, f"PREMISE: the push from that worktree skips the guards: {push.stderr}"
        if where == "GIT_DIR":
            monkeypatch.setenv("GIT_DIR", str(repo / ".git"))

        wired, why = mod._prepush_guards_wired()
        assert wired is None and "core.hooksPath" in why and "only-this-tree" in why, why

    @pytest.mark.parametrize("fails", ["the-linked-worktree-only", "finding-the-common-dir"])
    def test_git_failing_for_another_worktree_is_unmeasurable(self, tmp_path, monkeypatch, fails):
        """#2772: the worktrees are read one by one, so a read that fails for
        any of them, or failing to find them at all, is None — not the verdict
        of the checkouts that could be read."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        assert self._install_guards(repo).returncode == 0
        subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tmp_path / "wt")],
            check=True)
        monkeypatch.chdir(repo)
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"CONTROL: wired with a readable linked worktree: {why!r}"
        real = shutil.which("git")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        pattern = {"the-linked-worktree-only": '*"/worktrees/"*" config --get core.hooksPath "*',
                   "finding-the-common-dir": '*" rev-parse --git-common-dir "*'}[fails]
        (bindir / "git").write_text(
            "#!/bin/sh\n"
            f'case " $* " in {pattern}) exit 5 ;; esac\n'
            f'exec "{real}" "$@"\n', encoding="utf-8", newline="\n")
        (bindir / "git").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

        wired, why = mod._prepush_guards_wired()
        assert wired is None and "rc=5" in why, why

    def test_git_config_failing_to_read_hooks_path_changes_nothing(self, tmp_path, monkeypatch):
        """When git cannot say whether core.hooksPath is set, neither side
        takes it for unset: the judgement is None and the installer stops
        with nothing changed."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        monkeypatch.chdir(repo)
        real = shutil.which("git")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        (bindir / "git").write_text(
            "#!/bin/sh\n"
            'case " $* " in *" config --get core.hooksPath "*) exit 5 ;; esac\n'
            f'exec "{real}" "$@"\n', encoding="utf-8", newline="\n")
        (bindir / "git").chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        before = sorted(p.name for p in (repo / ".git" / "hooks").iterdir())

        wired, why = mod._prepush_guards_wired()
        assert wired is None, why
        r = self._install_guards(repo)
        assert r.returncode == 2 and "cannot read core.hooksPath" in r.stderr, r.stderr
        assert sorted(p.name for p in (repo / ".git" / "hooks").iterdir()) == before

    @pytest.mark.parametrize("target", ["shared", "dangling"])
    def test_a_symlinked_hooks_directory_is_unmeasurable(self, tmp_path, monkeypatch, target):
        """#2696 by another road: .git/hooks itself may be a link to a directory
        other repositories share, where the shim would refuse their every push.
        Neither side goes through it, and nothing is written there."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        monkeypatch.chdir(repo)
        hooks = repo / ".git" / "hooks"
        shutil.rmtree(hooks)
        shared = tmp_path / "shared"
        if target == "shared":
            shared.mkdir()
        symlink_or_skip(shared, hooks)

        wired, why = mod._prepush_guards_wired()
        assert wired is None and "symlink" in why, why
        r = self._install_guards(repo)
        assert r.returncode == 1 and "symlink" in r.stderr, r.stderr
        assert (sorted(shared.iterdir()) if shared.exists() else None) == (
            [] if target == "shared" else None), "the installer wrote through the link"

    def test_a_dot_git_symlink_is_not_a_symlinked_hooks_directory(self, tmp_path, monkeypatch):
        """Only the hooks directory itself is refused. A repository whose .git is
        a link (hooks a plain directory behind it) is judged and installed."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        real = tmp_path / "gitdir"
        (repo / ".git").rename(real)
        symlink_or_skip(real, repo / ".git")
        monkeypatch.chdir(repo)

        wired, why = mod._prepush_guards_wired()
        assert wired is False, why
        assert self._install_guards(repo).returncode == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is True, why

    def test_a_linked_worktree_sees_the_shared_hook(self, tmp_path, monkeypatch):
        """git runs the common .git/hooks for every worktree, so the judgement
        from a linked worktree reads that hook, not one under .git/worktrees/."""
        mod = _load()
        repo = tmp_path / "repo"
        repo.mkdir()
        self._repo(repo)
        wt = tmp_path / "wt"
        subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt)],
            check=True)
        assert self._install_guards(repo).returncode == 0
        monkeypatch.chdir(wt)

        wired, why = mod._prepush_guards_wired()
        assert wired is True, why

    @pytest.mark.parametrize("damage", ["missing", "not-utf8"])
    def test_an_installer_that_cannot_be_read_is_unmeasurable(
        self, tmp_path, monkeypatch, damage
    ):
        """#2760: reading the installer fails outright — it is gone, or it is
        not UTF-8. The verdict is None, not a traceback."""
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        installer = tmp_path / "scripts" / "ops" / "install_prepush_hook.sh"
        monkeypatch.setattr(mod, "_INSTALLER", installer)
        wired, _ = mod._prepush_guards_wired()
        assert wired is True, "CONTROL: it must be wired with the intact installer"

        if damage == "missing":
            installer.unlink()
        else:
            installer.write_bytes(b"\xff\xfe" + installer.read_bytes())
        wired, why = mod._prepush_guards_wired()
        assert wired is None and "量不到" in why, why

    @pytest.mark.parametrize("installer", [
        "#!/usr/bin/env bash\n",
        "x <<'VIBE_SHIM_EOF'\na\nVIBE_SHIM_EOF\ny <<'VIBE_SHIM_EOF'\nb\nVIBE_SHIM_EOF\n",
        "VIBE_SHIM_EOF\nx <<'VIBE_SHIM_EOF'\na\n",
    ], ids=["no-heredoc", "two-heredocs", "end-before-start"])
    def test_an_installer_without_the_shim_is_unmeasurable_not_unwired(
        self, tmp_path, monkeypatch, installer
    ):
        """The shim is read out of the installer. If that fails the verdict is
        None — "not installed" would prescribe an installer that cannot be
        read either."""
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._install_guards(tmp_path).returncode == 0
        wired, _ = mod._prepush_guards_wired()
        assert wired is True, "CONTROL: it must be wired with the real installer"

        broken = tmp_path / "installer.sh"
        broken.write_text(installer, encoding="utf-8", newline="\n")
        monkeypatch.setattr(mod, "_INSTALLER", broken)
        wired, why = mod._prepush_guards_wired()
        assert wired is None, why
        assert "VIBE_SHIM_EOF" in why, why


class TestMarkerPython:
    def test_write_marker_creates_file(self, tmp_path, monkeypatch):
        mod = _load()
        sha = _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        p, problem = mod.write_marker(tmp_path)
        assert problem is None
        assert p is not None
        assert p.exists()
        assert p.name == f".preflight-ok.{sha}"
        assert p.parent.name == ".git"

    def test_write_marker_is_idempotent(self, tmp_path, monkeypatch):
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        p1, _ = mod.write_marker(tmp_path)
        p2, _ = mod.write_marker(tmp_path)
        assert p1 == p2
        assert p1.exists()

    @pytest.mark.parametrize("break_it", ["no-head", "cannot-touch"])
    def test_write_marker_reports_why_it_could_not_write(
        self, tmp_path, monkeypatch, break_it
    ):
        """⛔ Same contract as `clear_marker`: no silent "could not".

        Both failure modes, because only one of them was covered and the other
        survived mutation.
        """
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        if break_it == "no-head":
            monkeypatch.setattr(mod, "_head_sha", lambda repo_root: None)
        else:
            def _refuse(self, *a, **kw):
                raise PermissionError(13, "in use")
            monkeypatch.setattr(Path, "touch", _refuse)

        written, problem = mod.write_marker(tmp_path)

        assert written is None
        assert problem, "a write that did not happen was reported as fine"

    def test_clear_marker_removes_head_and_leaves_other_commits_alone(
        self, tmp_path, monkeypatch
    ):
        """#1917: the radius is ONE commit, not the whole shared git dir."""
        mod = _load()
        sha = _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        git_dir = tmp_path / ".git"
        (git_dir / f".preflight-ok.{sha}").touch()
        (git_dir / ".preflight-ok.aaa").touch()
        (git_dir / ".preflight-ok.bbb").touch()
        # Unrelated file must survive.
        (git_dir / "config").touch(exist_ok=True)

        removed, problem = mod.clear_marker(tmp_path)

        assert problem is None
        assert removed is not None
        assert removed.name == f".preflight-ok.{sha}"
        assert not removed.exists()
        # Must-survive control group — the #1917 defect deleted these too.
        assert (git_dir / ".preflight-ok.aaa").exists()
        assert (git_dir / ".preflight-ok.bbb").exists()
        assert (git_dir / "config").exists()

    def test_clear_marker_reports_none_when_head_has_no_marker(
        self, tmp_path, monkeypatch
    ):
        """Nothing to remove ⇒ (None, None) — ⛔ and NOT a reported problem."""
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".git" / ".preflight-ok.aaa").touch()
        assert mod.clear_marker(tmp_path) == (None, None)
        assert (tmp_path / ".git" / ".preflight-ok.aaa").exists()

    def test_clear_on_empty(self, tmp_path, monkeypatch):
        mod = _load()
        _init_git(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert mod.clear_marker(tmp_path) == (None, None)

    def test_a_marker_written_in_a_worktree_lands_in_the_shared_git_dir(
        self, tmp_path, monkeypatch
    ):
        """⛔ The writer and the pre-push gate must agree on ONE directory.

        Keyed to `--git-dir`, a marker written while inside a linked worktree
        landed in `.git/worktrees/<name>/` and no other checkout could see it
        — so a commit that had passed preflight was blocked, and the banner's
        recovery instruction could not reach green from anywhere. A marker is
        a claim about a COMMIT; it does not belong to one worktree.
        """
        _init_git(tmp_path)
        wt = tmp_path.parent / "wt-marker"
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "worktree", "add", "-q", "--detach",
             str(wt), "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).returncode == 0

        mod = _load()
        monkeypatch.chdir(wt)
        p = mod.marker_path(wt, "deadbeef")
        private = subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()

        assert "worktrees" not in str(p), (
            f"the marker landed in the per-worktree git dir: {p}"
        )
        assert str(p.parent) != private, (
            "writer and gate would disagree: the gate reads the shared dir"
        )


class TestFailPathClearRadius:
    """#1917 — drive `main()`'s real branches and look at the shared git dir.

    ⛔ Behavioural, not a scan of the helpers: the defect was the PAIR (the call
    in `main()`) × (the radius in `clear_marker`). Only the checks are stubbed;
    the repo, the linked worktree, the shared dir and both helpers are real.
    """

    _OTHER_A = "a" * 40
    _OTHER_B = "b" * 40

    def _drive(self, mod, monkeypatch, wt, status):
        """Run main() from inside `wt` with every check forced to `status`."""
        stub_checks(monkeypatch, mod, lambda _n: status)
        monkeypatch.setattr(mod, "find_repo_root", lambda: wt)
        monkeypatch.setattr(os, "chdir", lambda p: None)
        monkeypatch.setattr(sys, "argv", ["pr_preflight.py"])
        return mod.main()

    def _setup(self, tmp_path, plant_head=True):
        """Main repo + one linked worktree. Returns (wt, shared_git_dir, sha).

        ⛔ `plant_head=False` for any test asserting HEAD's marker EXISTS after
        the run — planted, that assertion is held up by the fixture.
        """
        sha = _init_git(tmp_path)
        # ⛔ Unique per test: tmp_path.parent is shared across the class, and a
        # reused name makes `worktree add` exit 128 on the second test.
        wt = tmp_path.parent / f"wt-1917-{tmp_path.name}"
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "worktree", "add", "-q", "--detach",
             str(wt), "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).returncode == 0
        shared = tmp_path / ".git"
        # Two markers standing in for other commits — in this repo, other
        # sessions' worktrees.
        (shared / f".preflight-ok.{self._OTHER_A}").touch()
        (shared / f".preflight-ok.{self._OTHER_B}").touch()
        if plant_head:
            # An earlier PASS on this very commit.
            (shared / f".preflight-ok.{sha}").touch()
        return wt, shared, sha

    def test_a_failing_run_in_one_worktree_spares_other_commits_markers(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _load()
        wt, shared, sha = self._setup(tmp_path)

        rc = self._drive(mod, monkeypatch, wt, mod.Status.FAIL)

        assert rc == mod.EXIT_VIOLATION, "the run under test must actually FAIL"
        # Must-go: this commit's own marker (an earlier pass no longer holds).
        assert not (shared / f".preflight-ok.{sha}").exists()
        # ⛔ Must-survive control group. Before #1917 both of these were gone,
        # and the run reported them as "stale".
        for other in (self._OTHER_A, self._OTHER_B):
            assert (shared / f".preflight-ok.{other}").exists(), (
                f"another commit's marker ({other[:7]}) was deleted — "
                "the #1917 radius is back"
            )
        assert "stale" not in capsys.readouterr().out, (
            "nothing here determines staleness, so the word must not be printed"
        )

    @pytest.mark.parametrize("status_name", ["PASS", "WARN"])
    def test_a_non_failing_run_writes_this_commits_marker_and_removes_nothing(
        self, tmp_path, monkeypatch, status_name
    ):
        """The whole point of the mechanism: a clean run earns a marker.

        ⛔ HEAD's marker is NOT planted, so `exists()` here means `main()` wrote
        it. WARN is a pole because the branch turns on `has_failure`, which WARN
        must not trip (#1472).
        """
        mod = _load()
        wt, shared, sha = self._setup(tmp_path, plant_head=False)

        rc = self._drive(mod, monkeypatch, wt, getattr(mod.Status, status_name))

        assert rc == mod.EXIT_OK
        assert (shared / f".preflight-ok.{sha}").exists(), (
            f"a {status_name}-only run left no marker — the next push is "
            "blocked although preflight was clean"
        )
        # Must-not-fire control: the clear path belongs to FAIL only.
        for other in (self._OTHER_A, self._OTHER_B):
            assert (shared / f".preflight-ok.{other}").exists()
        assert len(list(shared.glob(".preflight-ok.*"))) == 3

    def test_an_undecidable_head_is_reported_not_silently_skipped(
        self, tmp_path, monkeypatch, capsys
    ):
        """⛔ git unavailable ⇒ say so. Silence here reads as "revoked"."""
        mod = _load()
        wt, shared, sha = self._setup(tmp_path)
        monkeypatch.setattr(mod, "_head_sha", lambda repo_root: None)

        rc = self._drive(mod, monkeypatch, wt, mod.Status.FAIL)
        out = capsys.readouterr().out

        assert rc == mod.EXIT_VIOLATION
        assert (shared / f".preflight-ok.{sha}").exists(), "precondition"
        assert "未能撤銷" in out, (
            "the marker survived a FAIL and nothing was printed — the push "
            "will be allowed and the operator has no way to know"
        )

    def test_a_pass_that_could_not_write_its_marker_is_reported(
        self, tmp_path, monkeypatch, capsys
    ):
        """⛔ READY + rc 0 + no marker, silently, is a dead end: the push is
        then refused by a banner telling the operator to run what they just ran.
        """
        mod = _load()
        wt, shared, sha = self._setup(tmp_path, plant_head=False)
        monkeypatch.setattr(mod, "_head_sha", lambda repo_root: None)

        rc = self._drive(mod, monkeypatch, wt, mod.Status.PASS)
        out = capsys.readouterr().out

        assert rc == mod.EXIT_OK
        assert not (shared / f".preflight-ok.{sha}").exists(), "precondition"
        assert "未能寫入" in out, (
            "the run said READY and wrote nothing, without saying so"
        )

    def test_a_marker_that_cannot_be_removed_is_reported(
        self, tmp_path, monkeypatch, capsys
    ):
        """Same class as the undecidable HEAD: could-not must not read as done."""
        mod = _load()
        wt, shared, sha = self._setup(tmp_path)

        def _refuse(self, *a, **kw):
            raise PermissionError(13, "in use")

        monkeypatch.setattr(Path, "unlink", _refuse)

        rc = self._drive(mod, monkeypatch, wt, mod.Status.FAIL)
        out = capsys.readouterr().out

        assert rc == mod.EXIT_VIOLATION
        assert (shared / f".preflight-ok.{sha}").exists(), "precondition"
        assert "未能撤銷" in out and "PermissionError" in out


@_BASH_SCRIPT_SKIP
class TestGateScript:
    """End-to-end behavioural tests of require_preflight_pass.sh."""

    def _run_gate(self, repo: Path, stdin: str, env_extra: dict | None = None):
        env = {**os.environ}
        if env_extra:
            env.update(env_extra)
        return subprocess.run(  # subprocess-timeout: ignore
            [_BASH, str(_SH_SCRIPT)],
            cwd=repo, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
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
        assert "scripts/tools/dx/pr_preflight.py" in r.stderr

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
