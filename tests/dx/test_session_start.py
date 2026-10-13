"""`.claude/hooks/session-start.sh` — the pre-push guards come first, every run.

Each test runs the real script in a throwaway repo, with the marker redirected
through `VIBE_SESSION_START_MARKER`. `pip`, `npm` and `pre-commit` on PATH are
stand-ins that only record their arguments, so nothing is installed. `helm` and
`golangci-lint` are stand-ins that report a version (the pinned one unless a
test says otherwise), `curl` is one that fails or serves bytes that are not the
pinned tarball, and `VIBE_SESSION_START_BIN` points into the test's tmp dir —
so no run downloads anything or writes outside it.
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _platform_fs import symlink_or_skip  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASH = shutil.which("bash") or "bash"
_OPS = (
    "_prepush_refs.sh", "protect_main_push.sh", "require_preflight_pass.sh",
    "pre_push_mkdocs_strict.sh", "prepush_dispatch.sh", "install_prepush_hook.sh",
)
_SCRIPT_TEXT = (_REPO_ROOT / ".claude" / "hooks" / "session-start.sh").read_text(encoding="utf-8")


def _pin(name: str) -> str:
    found = re.findall(rf"^{name}_VERSION=(\S+)$", _SCRIPT_TEXT, re.MULTILINE)
    assert len(found) == 1, f"{name}_VERSION in session-start.sh: {found}"
    return found[0]


_HELM = _pin("HELM")
_GOLANGCI = _pin("GOLANGCI_LINT")

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="cloud-only script (bash)")


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "pr_preflight", _REPO_ROOT / "scripts" / "tools" / "dx" / "pr_preflight.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(root: Path) -> Path:
    repo = root / "repo"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],  # subprocess-timeout: ignore
                   check=True, env=env)
    (repo / "scripts" / "ops").mkdir(parents=True)
    for name in _OPS:
        shutil.copy2(_REPO_ROOT / "scripts" / "ops" / name, repo / "scripts" / "ops" / name)
    (repo / ".claude" / "hooks").mkdir(parents=True)
    shutil.copy2(_REPO_ROOT / ".claude" / "hooks" / "session-start.sh",
                 repo / ".claude" / "hooks" / "session-start.sh")
    (repo / "requirements").mkdir()
    (repo / "requirements" / "ci-constraints.txt").write_text("", encoding="utf-8")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"],  # subprocess-timeout: ignore
                   check=True, env=env)
    subprocess.run(  # subprocess-timeout: ignore
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "commit", "-q", "-m", "init"], check=True, env=env)
    return repo


_INSTALL_SAID = "stand-in pre-commit install ran"


def _run(root: Path, repo: Path, marker_text: str | None, pre_commit_install_rc: int = 0,
         pre_commit: bool = True, real_pre_commit: bool = False,
         helm: str = _HELM, golangci: str = _GOLANGCI, curl: str = "fail"):
    """Run the script; return (completed, marker text, {tool: [argument lines]}).
    `pre_commit=False` leaves no pre-commit anywhere on PATH; `real_pre_commit`
    puts the installed one there (still recorded). The stand-in's `install`
    prints `_INSTALL_SAID` to stdout, as the real one reports there. `helm` and
    `golangci` are the versions their stand-ins report; `curl` is "fail" (exit
    22, as `curl -f` does on an HTTP error) or "garbage" (writes bytes that are
    not the pinned tarball to its `-o` file)."""
    marker = root / "marker"
    if marker_text is not None:
        marker.write_text(marker_text, encoding="utf-8")
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("pip", "npm", "pre-commit") if pre_commit else ("pip", "npm"):
        rc = (f'[ "$1" = install ] && echo {_INSTALL_SAID} && exit {pre_commit_install_rc}\n'
              if tool == "pre-commit" else "")
        if tool == "pre-commit" and real_pre_commit:
            rc = f'exec "{sys.executable}" -m pre_commit "$@"\n'
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "$*" >> "{root / tool}.calls"\n{rc}exit 0\n',
                        encoding="utf-8")
        stub.chmod(0o755)
    reports = {"helm": f"v{helm}+gstub",
               "golangci-lint": f"golangci-lint has version {golangci} built with stub"}
    for tool, line in reports.items():
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{line}"\n', encoding="utf-8")
        stub.chmod(0o755)
    body = ('exit 22\n' if curl == "fail" else
            'while [ $# -gt 0 ]; do [ "$1" = -o ] && echo garbage > "$2"; shift; done\n')
    stub = bindir / "curl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{root / "curl"}.calls"\n{body}', encoding="utf-8")
    stub.chmod(0o755)
    tool_bin = root / "tool-bin"
    tool_bin.mkdir(exist_ok=True)
    env = {**os.environ, "CLAUDE_CODE_REMOTE": "true", "CLAUDE_PROJECT_DIR": str(repo),
           "VIBE_SESSION_START_MARKER": str(marker), "VIBE_SESSION_START_BIN": str(tool_bin),
           "PATH": os.pathsep.join([str(bindir)] + [
               d for d in os.environ["PATH"].split(os.pathsep)
               if pre_commit or not (Path(d) / "pre-commit").exists()])}
    r = subprocess.run(  # subprocess-timeout: ignore
        [_BASH, ".claude/hooks/session-start.sh"], cwd=repo, env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    calls = {}
    for tool in ("pip", "npm", "pre-commit", "curl"):
        f = root / f"{tool}.calls"
        calls[tool] = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
    text = marker.read_text(encoding="utf-8") if marker.exists() else ""
    return r, text, calls


def _installs(calls: list[str]) -> list[str]:
    """`pre-commit install …` calls, whatever their flags (not install-hooks)."""
    return [c for c in calls if c.split()[:1] == ["install"]]


def _result(marker: str) -> list[str]:
    return [line for line in marker.splitlines() if line.startswith("RESULT=")]


def _require_pre_commit():
    # Same flag as tests/dx/test_preflight_marker.py: under it a missing
    # pre_commit fails instead of skipping.
    if os.environ.get("VIBE_REQUIRE_PRE_COMMIT") == "1":
        assert importlib.util.find_spec("pre_commit") is not None
    else:
        pytest.importorskip("pre_commit")


def test_an_already_bootstrapped_container_gets_its_guards_back(tmp_path, monkeypatch):
    """#2761: the no-op path used to count any .git/hooks/pre-push as wired, so
    pre-commit's pre-push template stayed and a push to main went unguarded."""
    _require_pre_commit()
    repo = _repo(tmp_path)
    for args in ((), ("--hook-type", "pre-push")):
        subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "pre_commit", "install", *args],
            cwd=repo, check=True, capture_output=True)
    mod = _load_preflight()
    monkeypatch.chdir(repo)
    assert mod._prepush_guards_wired()[0] is False

    r, marker, calls = _run(tmp_path, repo, "RESULT=ok\n")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "already bootstrapped" in r.stdout and calls["pip"] == []
    # #2790: even a commit hook pre-commit wrote, executable, is reinstalled.
    assert _installs(calls["pre-commit"]) == ["install"], calls["pre-commit"]
    wired, why = mod._prepush_guards_wired()
    assert wired is True, why
    assert _result(marker) == ["RESULT=ok"]


def test_a_refusal_in_a_bootstrapped_container_reruns_everything_and_fails(tmp_path):
    """A hook the installer refuses is reported, not skipped; the marker is
    rewritten, so no RESULT=ok from the earlier run is left next to the failure."""
    repo = _repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    mine = "#!/bin/sh\necho mine\n"
    (hooks / "pre-push").write_text(mine, encoding="utf-8")

    r, marker, calls = _run(tmp_path, repo, "RESULT=ok\n")

    assert r.returncode == 1, r.stdout + r.stderr
    assert "already bootstrapped" not in r.stdout
    results = _result(marker)
    assert len(results) == 1 and "install_prepush_hook" in results[0], marker
    assert calls["pip"], "a refusal skipped the Python deps"
    assert _installs(calls["pre-commit"]) == []
    assert (hooks / "pre-push").read_text(encoding="utf-8") == mine


@pytest.mark.parametrize("setup", ["symlinked-hooks", "empty-hooks-path"])
def test_a_refusal_skips_pre_commit_install_and_nothing_else(tmp_path, setup):
    """When the installer refuses, `pre-commit install` does not run, so nothing
    is written through the link or into hooks git will not run. The Python deps
    still are installed."""
    repo = _repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    if setup == "symlinked-hooks":
        shared = tmp_path / "shared"
        shared.mkdir()
        shutil.rmtree(hooks)
        symlink_or_skip(shared, hooks)
        watched = shared
    else:
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ""],  # subprocess-timeout: ignore
                       check=True)
        watched = hooks
    before = sorted(p.name for p in watched.iterdir())

    r, marker, calls = _run(tmp_path, repo, None)

    assert r.returncode == 1, r.stdout + r.stderr
    results = _result(marker)
    assert len(results) == 1 and "install_prepush_hook" in results[0], marker
    assert _installs(calls["pre-commit"]) == []
    assert calls["pip"], "a refusal skipped the Python deps"
    assert sorted(p.name for p in watched.iterdir()) == before


@pytest.mark.parametrize("state", [
    "last-run-failed", "no-pre-commit-on-PATH", "e2e-deps-gone", "helm-off-pin",
    "golangci-lint-off-pin"])
def test_a_no_op_needs_every_condition(tmp_path, state):
    """The no-op path is taken only after a RESULT=ok run, with pre-commit on
    PATH, the e2e deps still in place and helm / golangci-lint at their pins;
    otherwise the whole script runs again."""
    repo = _repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    if state == "e2e-deps-gone":
        (repo / "tests" / "e2e").mkdir(parents=True)
        (repo / "tests" / "e2e" / "package.json").write_text("{}\n", encoding="utf-8")
    marker_text = ("RESULT=failed (not importable: mkdocs)\n" if state == "last-run-failed"
                   else "RESULT=ok\n")

    r, _, calls = _run(tmp_path, repo, marker_text, pre_commit=state != "no-pre-commit-on-PATH",
                       helm="3.0.0" if state == "helm-off-pin" else _HELM,
                       golangci="2.5.0" if state == "golangci-lint-off-pin" else _GOLANGCI)

    assert "already bootstrapped" not in r.stdout
    assert calls["pip"]
    if state != "no-pre-commit-on-PATH":
        assert _installs(calls["pre-commit"]) == ["install"], calls["pre-commit"]
        assert _INSTALL_SAID in r.stdout


@pytest.mark.parametrize("commit_hook", [True, False])
def test_a_linked_worktree_no_ops_and_reinstalls_the_commit_hook(tmp_path, commit_hook):
    """#2775: in a linked worktree `.git` is a file, so a literal
    `.git/hooks/pre-commit` check never held and every start reran `npm ci`.
    #2790: the commit hook is reinstalled on the no-op path, not looked for."""
    repo = _repo(tmp_path)
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(wt)],  # subprocess-timeout: ignore
                   check=True)
    assert (wt / ".git").is_file()
    if commit_hook:
        (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")

    r, marker, calls = _run(tmp_path, wt, "RESULT=ok\n")

    assert r.returncode == 0, r.stdout + r.stderr
    assert "already bootstrapped" in r.stdout, r.stdout + r.stderr
    assert calls["pip"] == [] and calls["npm"] == []
    assert _installs(calls["pre-commit"]) == ["install"]
    assert _result(marker) == ["RESULT=ok"]


@pytest.mark.parametrize("commit_hook", ["0644", "absent"])
def test_a_no_op_reinstalls_a_commit_hook_git_would_not_run(tmp_path, commit_hook):
    """#2790: a commit hook that exists may still not run (0644), and none at
    all is the same. The no-op path runs `pre-commit install` either way."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    if commit_hook == "0644":
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        hook.chmod(0o644)

    r, marker, calls = _run(tmp_path, repo, "RESULT=ok\n")

    assert r.returncode == 0, r.stdout + r.stderr
    assert _installs(calls["pre-commit"]) == ["install"], r.stdout + r.stderr
    assert "already bootstrapped" in r.stdout and calls["pip"] == []
    assert _INSTALL_SAID in r.stdout, "pre-commit install's own report was hidden"
    assert _result(marker) == ["RESULT=ok"]


def test_a_no_op_makes_a_0644_commit_hook_run_again(tmp_path):
    """#2790 with the real pre-commit: after the no-op, git runs the commit
    hook again (pre-commit rewrites it executable)."""
    _require_pre_commit()
    repo = _repo(tmp_path)
    subprocess.run(  # subprocess-timeout: ignore
        [sys.executable, "-m", "pre_commit", "install"], cwd=repo, check=True,
        capture_output=True)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.chmod(0o644)

    r, marker, _ = _run(tmp_path, repo, "RESULT=ok\n", real_pre_commit=True)

    assert r.returncode == 0 and "already bootstrapped" in r.stdout, r.stdout + r.stderr
    assert os.access(hook, os.X_OK), "the no-op left a commit hook git does not run"
    assert _result(marker) == ["RESULT=ok"]


def test_a_failed_reinstall_does_not_no_op(tmp_path):
    """#2790: when `pre-commit install` fails on the no-op path the whole
    script runs and the failure is what the marker ends with."""
    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)

    r, marker, calls = _run(tmp_path, repo, "RESULT=ok\n", pre_commit_install_rc=97)

    assert r.returncode == 1, r.stdout + r.stderr
    assert "already bootstrapped" not in r.stdout
    assert calls["pip"], "a failed reinstall no-opped"
    assert _result(marker) == ["RESULT=failed (pre-commit install)"]


def test_a_failed_pre_commit_install_is_recorded(tmp_path):
    repo = _repo(tmp_path)

    r, marker, _ = _run(tmp_path, repo, None, pre_commit_install_rc=97)

    assert r.returncode == 1, r.stdout + r.stderr
    assert _result(marker) == ["RESULT=failed (pre-commit install)"]


def test_an_earlier_stop_still_names_the_installer_failure(tmp_path):
    """A run that stops early for another reason still says the guards failed."""
    repo = _repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", ""],  # subprocess-timeout: ignore
                   check=True)
    (repo / "requirements" / "ci-constraints.txt").write_text(
        "--index-url http://127.0.0.1:9/simple\n", encoding="utf-8")

    r, marker, calls = _run(tmp_path, repo, None)

    assert r.returncode == 1, r.stdout + r.stderr
    assert _result(marker) == [
        "RESULT=failed (install_prepush_hook; constraints file sets a package index)"]
    assert calls["pip"] == []


def test_tools_already_at_their_pins_are_not_downloaded(tmp_path):
    """#2955: a container that has both binaries at their pins downloads
    nothing, records the versions and puts neither tool in RESULT.
    ⛔ Not `RESULT=ok`: the import check runs against the REAL interpreter
    (pip is a stand-in), so a runner without mkdocs ends `failed` for a reason
    that is not this test's."""
    repo = _repo(tmp_path)

    r, marker, calls = _run(tmp_path, repo, None)

    assert calls["curl"] == []
    assert f"helm={_HELM}" in marker.splitlines()
    assert f"golangci-lint={_GOLANGCI}" in marker.splitlines()
    results = _result(marker)
    assert len(results) == 1 and "helm" not in results[0] \
        and "golangci-lint" not in results[0], r.stdout + r.stderr


@pytest.mark.parametrize("curl,why", [("fail", "download"), ("garbage", "sha256 mismatch")])
def test_a_tool_that_cannot_be_installed_fails_the_run(tmp_path, curl, why):
    """#2955: helm off its pin and no way to get the pinned one is RESULT=failed,
    with the reason — not a session that silently skips tests/helm. Bytes that
    are not the pinned tarball are never installed."""
    repo = _repo(tmp_path)

    r, marker, calls = _run(tmp_path, repo, None, helm="3.0.0", curl=curl)

    assert r.returncode == 1, r.stdout + r.stderr
    # Ends with, not equals: a runner without mkdocs adds its own reason first.
    results = _result(marker)
    assert len(results) == 1 and results[0].startswith("RESULT=failed (") \
        and results[0].endswith(f"helm: {why})"), marker
    assert f"helm=FAILED ({why})" in marker.splitlines()
    assert any(f"helm-v{_HELM}-linux-amd64.tar.gz" in c for c in calls["curl"]), calls["curl"]
    assert not (tmp_path / "tool-bin" / "helm").exists()
    assert calls["pip"], "a tool failure skipped the Python deps"
