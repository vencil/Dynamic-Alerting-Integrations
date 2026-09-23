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

import ast
import importlib.util
import os
import shutil
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
# (TestMarkerPython / TestFailPathClearRadius) DO run cross-platform.
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


class TestPrepushWiring:
    """`pr_preflight._prepush_guards_wired` — are the guards on the push path?

    ⛔ This class used to assert that `pre-commit install --hook-type pre-push`
    counts as installed. #1689 made that FALSE and this is where the flip is
    recorded: a hook run by pre-commit is handed exactly one refspec, so a push
    carrying `feat/x` and `main` reached the direct-push guard as `feat/x` and
    printed Passed while main moved on the remote. The old assertion was a test
    holding a defect in place.

    There are now TWO wired states, because there are two install orders and
    both really happen:

        shim at .git/hooks/pre-push                     (installer ran alone)
        pre-commit's template + shim at pre-push.legacy (pre-commit ran after,
                                                         or before, the installer)

    and one state that looks wired and is not: pre-commit's template with
    nothing behind it. `pre-commit install -f --hook-type pre-push` produces it
    by DELETING pre-push.legacy — measured: rc=0, and the output says only
    "pre-commit installed at …", not one word about the removal. That is the
    case the last test pins.
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
        # The probe shells out to the repo's own installer, so the temp repo has
        # to carry it. Copied, not referenced by absolute path: that is the
        # production file, re-read from disk on every run.
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
            cwd=tmp_path, capture_output=True, text=True,
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

        wired, _ = mod._prepush_guards_wired()
        assert wired is False, "no hooks at all"

        assert self._precommit_install(tmp_path) == 0
        wired, _ = mod._prepush_guards_wired()
        assert wired is False, (
            "`pre-commit install` on its own installs the pre-commit hook only"
        )

        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        wired, why = mod._prepush_guards_wired()
        assert wired is False, (
            "pre-commit's own pre-push hook is NOT the wiring any more (#1689): "
            f"it is handed one refspec. why={why!r}"
        )

    def test_the_installer_wires_it_in_either_order(self, tmp_path, monkeypatch):
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        r = self._install_guards(tmp_path)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        wired, why = mod._prepush_guards_wired()
        assert wired is True, why
        assert (tmp_path / ".git" / "hooks" / "pre-push").exists()

        # …and pre-commit arriving afterwards must not break it: it migrates the
        # shim to pre-push.legacy and calls it with the FULL stdin.
        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        assert (tmp_path / ".git" / "hooks" / "pre-push.legacy").exists()
        wired, why = mod._prepush_guards_wired()
        assert wired is True, f"the migrated state must count as wired: {why!r}"

    def test_installer_after_precommit_does_not_clobber_the_template(
        self, tmp_path, monkeypatch
    ):
        """The other order. ⛔ The installer must not overwrite
        .git/hooks/pre-push here — doing so would silently drop every
        pre-commit-stage hook, trading one silent gap for another."""
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        template = hook.read_text(encoding="utf-8")

        r = self._install_guards(tmp_path)
        assert r.returncode == 0, f"{r.stdout}{r.stderr}"
        assert hook.read_text(encoding="utf-8") == template, (
            "the installer overwrote pre-commit's hook file"
        )
        assert (tmp_path / ".git" / "hooks" / "pre-push.legacy").exists()
        wired, why = mod._prepush_guards_wired()
        assert wired is True, why

    def test_force_reinstall_disarms_it_and_the_probe_says_so(
        self, tmp_path, monkeypatch
    ):
        """`pre-commit install -f` deletes pre-push.legacy without saying so.

        This is the one failure mode the installer cannot prevent, which is
        exactly why the probe has to catch it: after -f the picture is a
        perfectly normal pre-commit hook, and every guard is gone.
        """
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)

        assert self._install_guards(tmp_path).returncode == 0
        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0
        wired, _ = mod._prepush_guards_wired()
        assert wired is True, "CONTROL: it must be wired before -f, or this proves nothing"

        assert self._precommit_install(tmp_path, "-f", "--hook-type", "pre-push") == 0
        assert not (tmp_path / ".git" / "hooks" / "pre-push.legacy").exists()
        wired, why = mod._prepush_guards_wired()
        assert wired is False, "a -f reinstall left the probe reporting wired"
        assert "pre-push.legacy" in why, (
            f"the explanation must name what went missing; got {why!r}"
        )

    def test_a_hand_written_prepush_hook_does_not_count(self, tmp_path, monkeypatch):
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        hook = tmp_path / ".git" / "hooks" / "pre-push"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8", newline="\n")
        wired, why = mod._prepush_guards_wired()
        assert wired is False
        assert "chained" in why, (
            f"the message must say what the installer will do with it: {why!r}"
        )

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

    def test_the_never_installed_case_is_not_diagnosed_as_a_force_reinstall(
        self, tmp_path, monkeypatch
    ):
        """⛔ Two different causes land in the same state and they are NOT
        distinguishable — so the message must not pick one.

        `pre-commit install -f` deletes pre-push.legacy silently, and "never
        installed" looks identical. The first version of this message asserted
        the -f story outright, which misdiagnosed every fresh clone — including
        the maintainer's own repo, which had simply never run the installer.
        """
        self._require_pre_commit()
        mod = _load()
        self._repo(tmp_path)
        monkeypatch.chdir(tmp_path)
        assert self._precommit_install(tmp_path, "--hook-type", "pre-push") == 0

        wired, why = mod._prepush_guards_wired()
        assert wired is False
        assert "從來沒安裝過" in why, f"the never-installed cause is missing: {why!r}"
        assert "-f" in why, f"the silent-delete cause is missing: {why!r}"


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

    def test_clear_marker_removes_head_and_leaves_other_commits_alone(
        self, tmp_path, monkeypatch
    ):
        """#1917: the radius is ONE commit, not the whole shared git dir.

        The two planted markers stand in for other commits — in this repo they
        are very likely other worktrees' (`_git_dir` is `--git-common-dir`).
        They are the must-survive control; HEAD's is the must-go one.
        """
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
        """Nothing to remove ⇒ (None, None) — ⛔ and NOT a reported problem.

        The two Nones are the difference between "there was nothing here" and
        "I could not tell / could not do it"; only the latter gets printed.
        """
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
            capture_output=True, text=True,
        ).returncode == 0

        mod = _load()
        monkeypatch.chdir(wt)
        p = mod.marker_path(wt, "deadbeef")
        private = subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
            capture_output=True, text=True,
        ).stdout.strip()

        assert "worktrees" not in str(p), (
            f"the marker landed in the per-worktree git dir: {p}"
        )
        assert str(p.parent) != private, (
            "writer and gate would disagree: the gate reads the shared dir"
        )


class TestFailPathClearRadius:
    """#1917 — drive `main()`'s real FAIL branch and look at the shared dir.

    ⛔ Behavioural, not a scan of `clear_marker`'s body. The defect was not the
    helper alone but the pair (unconditional call on FAIL) × (glob over the
    shared git dir), and only running the orchestrator exercises both. The
    checks are stubbed because reaching a real FAIL needs `gh`, network and a
    300s `pre-commit --all-files`; everything about markers — the git repo, the
    linked worktree, the shared dir, `_head_sha`, `clear_marker`,
    `write_marker` — is real.
    """

    _OTHER_A = "a" * 40
    _OTHER_B = "b" * 40

    @staticmethod
    def _checks_main_feeds_to_the_report():
        """Derive the check names from `main()`'s source, don't list them.

        ⛔ A hand-written tuple cannot notice a NINTH check being added: the new
        one would not be stubbed and would really run here — network, `gh`, or
        a 300s `pre-commit --all-files` — silently. Re-derived from the calls
        `main()` passes to `report.add(...)`, a new check is stubbed the day it
        lands. (An `assert hasattr` only ever caught renames.)
        """
        tree = ast.parse(_PY_SCRIPT.read_text(encoding="utf-8"))
        main_fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )
        names = set()
        for node in ast.walk(main_fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add"):
                continue
            for arg in node.args:
                if (isinstance(arg, ast.Call)
                        and isinstance(arg.func, ast.Name)
                        and arg.func.id.startswith("check_")):
                    names.add(arg.func.id)
        # ⛔ Must-fire control: an AST walk that silently yields nothing would
        # stub nothing and let every check run for real — "could not read the
        # source" must not look like "there are no checks".
        assert "check_branch_identity" in names, (
            f"derivation found no known check (got {sorted(names)}) — the "
            "shape of main() changed and this test is measuring nothing"
        )
        return names

    def _drive(self, mod, monkeypatch, wt, status):
        """Run main() from inside `wt` with every check forced to `status`."""
        for name in self._checks_main_feeds_to_the_report():
            assert hasattr(mod, name), f"check no longer exists: {name}"
            monkeypatch.setattr(
                mod, name,
                lambda *a, _n=name, **kw: mod.CheckResult(_n, status, "stubbed"),
            )
        monkeypatch.setattr(mod, "find_repo_root", lambda: wt)
        monkeypatch.setattr(os, "chdir", lambda p: None)
        monkeypatch.setattr(sys, "argv", ["pr_preflight.py"])
        return mod.main()

    def _setup(self, tmp_path, plant_head=True):
        """Main repo + one linked worktree. Returns (wt, shared_git_dir, sha).

        `plant_head=False` leaves HEAD's marker absent, so a test that asserts
        it exists afterwards is asserting that `main()` WROTE it — with it
        planted, that assertion is held up by the fixture and passes even if
        the write path is gone.
        """
        sha = _init_git(tmp_path)
        # ⛔ Unique per test: tmp_path.parent is shared across the class, and a
        # reused name makes `worktree add` exit 128 on the second test.
        wt = tmp_path.parent / f"wt-1917-{tmp_path.name}"
        assert subprocess.run(  # subprocess-timeout: ignore
            ["git", "-C", str(tmp_path), "worktree", "add", "-q", "--detach",
             str(wt), "HEAD"],
            capture_output=True, text=True,
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
        it. WARN is parametrised because it is the ordinary state, not an edge:
        a detached worktree alone makes `Branch identity` WARN, and the write /
        clear branch turns on `has_failure`, which WARN must not trip (#1472).
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
        """⛔ git unavailable ⇒ say so. Silence here reads as "revoked".

        Narrowing the radius introduced this case: the glob never needed to
        know which commit it was on, `clear_marker` does. `run()` turns a git
        timeout into rc 124 and the checks FAIL, so this path is reached on
        exactly the machine where git is too slow to answer.
        """
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
