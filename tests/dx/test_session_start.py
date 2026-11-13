"""`.claude/hooks/session-start.sh` — the pre-push guards come first, every run.

Each test runs the real script in a throwaway repo, with the marker redirected
through `VIBE_SESSION_START_MARKER`. `pip`, `npm` and `pre-commit` on PATH are
stand-ins that only record their arguments, so nothing is installed. `helm`,
`golangci-lint` and `hadolint` are stand-ins that report a version (the pinned
one unless a test says otherwise), `curl` is one that fails, serves bytes that
are not the pinned artifact, or serves a stand-in binary whose checksum a test
pinned, and `VIBE_SESSION_START_BIN` points into the test's tmp dir — so no run
downloads anything or writes outside it.
"""
from __future__ import annotations

import hashlib
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
_NODE = _pin("NODE")
_GOLANGCI = _pin("GOLANGCI_LINT")

# hadolint's pin is not in the script: it reads the lint wrapper's (#3627).
_HADOLINT_SRC = "scripts/tools/lint/check_iac_vibe_rules.py"
_HADOLINT_SRC_TEXT = (_REPO_ROOT / _HADOLINT_SRC).read_text(encoding="utf-8")


def _py_pin(name: str, text: str = _HADOLINT_SRC_TEXT) -> str:
    found = re.findall(rf'^{name}\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    assert len(found) == 1, f"{name} in {_HADOLINT_SRC}: {found}"
    return found[0]


_HADOLINT = _py_pin("HADOLINT_VERSION")

# kube-linter, pint and promtool: pins read from the files that own them (#3611).
_OWNED_PIN_SRCS = ("scripts/tools/lint/check_iac_helm.py", "scripts/tools/lint/check_pint.py",
                   ".devcontainer/install-promtool.sh")
_KUBE_LINTER = _py_pin("KUBE_LINTER_VERSION",
                       (_REPO_ROOT / _OWNED_PIN_SRCS[0]).read_text(encoding="utf-8"))
_PINT = _py_pin("PINT_VERSION", (_REPO_ROOT / _OWNED_PIN_SRCS[1]).read_text(encoding="utf-8"))
_PROMTOOL = re.findall(r"^PROM_VERSION=(\S+)$",
                       (_REPO_ROOT / _OWNED_PIN_SRCS[2]).read_text(encoding="utf-8"),
                       re.MULTILINE)[0]

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
    for src in (_HADOLINT_SRC, *_OWNED_PIN_SRCS):
        (repo / src).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / src, repo / src)
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
         helm: str = _HELM, golangci: str = _GOLANGCI, hadolint: str | None = _HADOLINT,
         curl: "str | bytes" = "fail", tool_bin_on_path: bool = False,
         kube_linter: str | None = _KUBE_LINTER, pint: str | None = _PINT,
         promtool: str | None = _PROMTOOL, node: str | None = _NODE):
    """Run the script; return (completed, marker text, {tool: [argument lines]}).
    `pre_commit=False` leaves no pre-commit anywhere on PATH; `real_pre_commit`
    puts the installed one there (still recorded). The stand-in's `install`
    prints `_INSTALL_SAID` to stdout, as the real one reports there. `helm` and
    `golangci` / `hadolint` are the versions their stand-ins report (`hadolint`
    None: a stand-in that fails to run, as if absent); `curl` is "fail" (exit 22, as `curl -f` does on
    an HTTP error), "garbage" (writes bytes that are not the pinned artifact to
    its `-o` file) or any other text, which it writes to its `-o` file verbatim.
    `tool_bin_on_path` puts the install target first on PATH, so what the
    script installs is what it then finds."""
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
    owned = {"hadolint": (hadolint, lambda v: f"Haskell Dockerfile Linter {v.removeprefix('v')}"),
             "kube-linter": (kube_linter, lambda v: v.removeprefix("v")),
             "pint": (pint, lambda v: f"{v} (revision: stub)"),
             "promtool": (promtool, lambda v: f"promtool, version {v} (branch: stub)"),
             "node": (node, lambda v: f"v{v}")}
    for tool, (version, line) in owned.items():
        if version is not None:
            reports[tool] = line(version)
    for tool, line in reports.items():
        stub = bindir / tool
        stub.write_text(f'#!/bin/sh\necho "{line}"\n', encoding="utf-8")
        stub.chmod(0o755)
    for tool, (version, _line) in owned.items():
        if version is None:
            # A stand-in that cannot run, not an absent file: a real binary
            # later on PATH (the runner's own) would otherwise answer at the pin.
            stub = bindir / tool
            stub.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
            stub.chmod(0o755)
    served = root / "curl.serves"
    if isinstance(curl, bytes):
        served.write_bytes(curl)
    else:
        served.write_text("garbage\n" if curl == "garbage" else curl, encoding="utf-8")
    body = ('exit 22\n' if curl == "fail" else
            f'while [ $# -gt 0 ]; do [ "$1" = -o ] && cat "{served}" > "$2"; shift; done\n')
    stub = bindir / "curl"
    stub.write_text(f'#!/bin/sh\necho "$*" >> "{root / "curl"}.calls"\n{body}', encoding="utf-8")
    stub.chmod(0o755)
    tool_bin = root / "tool-bin"
    tool_bin.mkdir(exist_ok=True)
    node_link = root / "node-link-bin"
    env = {**os.environ, "CLAUDE_CODE_REMOTE": "true", "CLAUDE_PROJECT_DIR": str(repo),
           "VIBE_SESSION_START_MARKER": str(marker), "VIBE_SESSION_START_BIN": str(tool_bin),
           "VIBE_SESSION_START_NODE_HOME": str(root / "node-home"),
           "VIBE_SESSION_START_NODE_LINK_BIN": str(node_link),
           "PATH": os.pathsep.join([str(node_link), str(tool_bin)] * tool_bin_on_path
                                   + [str(bindir)] + [
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
    "last-run-failed", "no-pre-commit-on-PATH", "e2e-deps-gone", "portal-deps-gone",
    "portal-deps-stale",
    "helm-off-pin",
    "golangci-lint-off-pin", "hadolint-off-pin", "hadolint-absent",
    "kube-linter-off-pin", "pint-off-pin", "promtool-absent",
    # One suffix away from the pin: an exact match, not a prefix or substring.
    "kube-linter-near-pin", "pint-near-pin", "promtool-near-pin",
    "node-off-pin", "node-near-pin", "node-absent"])
def test_a_no_op_needs_every_condition(tmp_path, state):
    """The no-op path is taken only after a RESULT=ok run, with pre-commit on
    PATH, the e2e deps still in place and helm / golangci-lint / hadolint at
    their pins;
    otherwise the whole script runs again."""
    repo = _repo(tmp_path)
    hooks = repo / ".git" / "hooks"
    (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
    if state == "e2e-deps-gone":
        (repo / "tests" / "e2e").mkdir(parents=True)
        (repo / "tests" / "e2e" / "package.json").write_text("{}\n", encoding="utf-8")
    if state == "portal-deps-gone":
        (repo / "tools" / "portal").mkdir(parents=True)
        (repo / "tools" / "portal" / "package.json").write_text("{}\n", encoding="utf-8")
    if state == "portal-deps-stale":
        # Installed, then the lockfile changed (another branch's bump): the
        # `make portal-deps` predicate — package-lock.json newer than
        # node_modules/.package-lock.json — must reinstall.
        portal = repo / "tools" / "portal"
        (portal / "node_modules").mkdir(parents=True)
        (portal / "package.json").write_text("{}\n", encoding="utf-8")
        installed = portal / "node_modules" / ".package-lock.json"
        installed.write_text("{}\n", encoding="utf-8")
        os.utime(installed, (1_000_000_000, 1_000_000_000))
        (portal / "package-lock.json").write_text("{}\n", encoding="utf-8")
    marker_text = ("RESULT=failed (not importable: mkdocs)\n" if state == "last-run-failed"
                   else "RESULT=ok\n")

    r, _, calls = _run(tmp_path, repo, marker_text, pre_commit=state != "no-pre-commit-on-PATH",
                       helm="3.0.0" if state == "helm-off-pin" else _HELM,
                       golangci="2.5.0" if state == "golangci-lint-off-pin" else _GOLANGCI,
                       hadolint={"hadolint-off-pin": "v2.10.0",
                                 "hadolint-absent": None}.get(state, _HADOLINT),
                       kube_linter={"kube-linter-off-pin": "v0.6.0",
                                    "kube-linter-near-pin": _KUBE_LINTER + "0"}.get(
                                        state, _KUBE_LINTER),
                       pint={"pint-off-pin": "0.70.0",
                             "pint-near-pin": _PINT + "1"}.get(state, _PINT),
                       promtool={"promtool-absent": None,
                                 "promtool-near-pin": _PROMTOOL + "0"}.get(state, _PROMTOOL),
                       node={"node-off-pin": "22.23.4", "node-near-pin": _NODE + "0",
                             "node-absent": None}.get(state, _NODE))

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
    assert f"hadolint={_HADOLINT}" in marker.splitlines()
    for tool, version in (("kube-linter", _KUBE_LINTER), ("pint", _PINT),
                          ("promtool", _PROMTOOL)):
        assert f"{tool}={version}" in marker.splitlines(), marker
    results = _result(marker)
    assert len(results) == 1 and "helm" not in results[0] \
        and "golangci-lint" not in results[0] and "hadolint" not in results[0], \
        r.stdout + r.stderr


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


# ── hadolint (#3627) ────────────────────────────────────────────────────────


def _set_hadolint_pin(repo: Path, **values: str) -> None:
    """Rewrite the copied lint wrapper's HADOLINT_* lines (the script's SSOT)."""
    f = repo / _HADOLINT_SRC
    text = f.read_text(encoding="utf-8")
    for name, value in values.items():
        text, n = re.subn(rf'^{name}\s*=\s*"[^"]+"\s*$', f'{name} = "{value}"', text,
                          flags=re.MULTILINE)
        assert n == 1, name
    f.write_text(text, encoding="utf-8")


def _hadolint_downloads(calls: list[str]) -> list[str]:
    return [c for c in calls if "hadolint" in c]


@pytest.mark.parametrize("curl,why", [("fail", "download"), ("garbage", "sha256 mismatch")])
def test_hadolint_that_cannot_be_installed_fails_the_run(tmp_path, curl, why):
    """#3627: without hadolint the iac-sast-check hook blocks, so preflight
    writes no marker and every push is refused. That is RESULT=failed with the
    reason, at the start of the session — not a surprise at the first push.
    Bytes that are not the pinned binary are never installed."""
    repo = _repo(tmp_path)

    r, marker, calls = _run(tmp_path, repo, None, hadolint=None, curl=curl)

    assert r.returncode == 1, r.stdout + r.stderr
    results = _result(marker)
    assert len(results) == 1 and results[0].endswith(f"hadolint: {why})"), marker
    assert f"hadolint=FAILED ({why})" in marker.splitlines()
    assert _hadolint_downloads(calls["curl"]) == [
        c for c in calls["curl"]
        if f"/download/{_HADOLINT}/hadolint-Linux-x86_64" in c], calls["curl"]
    assert _hadolint_downloads(calls["curl"]), calls["curl"]
    assert not (tmp_path / "tool-bin" / "hadolint").exists()


def test_hadolint_pin_is_read_from_the_lint_wrapper_at_run_time(tmp_path):
    """The script carries no hadolint version of its own: bump the wrapper and
    the script asks for the new one. A copy in the script would still fetch the
    old version here."""
    repo = _repo(tmp_path)
    _set_hadolint_pin(repo, HADOLINT_VERSION="v9.8.7")

    r, marker, calls = _run(tmp_path, repo, None)  # stand-in reports the real pin

    assert any("/download/v9.8.7/hadolint-Linux-x86_64" in c for c in calls["curl"]), \
        calls["curl"]
    assert "hadolint=FAILED (download)" in marker.splitlines(), marker


def test_hadolint_is_installed_as_a_bare_binary(tmp_path):
    """The release asset is the binary itself, not a tarball: what curl served
    (and the checksum matched) is what lands in the install dir and runs."""
    repo = _repo(tmp_path)
    binary = (f'#!/bin/sh\necho "Haskell Dockerfile Linter '
              f'{_HADOLINT.removeprefix("v")}"\n')
    _set_hadolint_pin(repo, HADOLINT_SHA256=hashlib.sha256(binary.encode()).hexdigest())

    r, marker, calls = _run(tmp_path, repo, None, hadolint=None, curl=binary,
                            tool_bin_on_path=True)

    installed = tmp_path / "tool-bin" / "hadolint"
    assert installed.read_text(encoding="utf-8") == binary, r.stdout + r.stderr
    assert os.access(installed, os.X_OK)
    assert f"hadolint={_HADOLINT}" in marker.splitlines(), marker
    assert all("hadolint" not in line for line in _result(marker)), marker


@pytest.mark.parametrize("damage", ["missing", "duplicated", "not-a-version", "not-a-sha"])
def test_an_unreadable_hadolint_pin_fails_without_downloading(tmp_path, damage):
    """Zero lines, two lines or a value of the wrong shape is a broken SSOT:
    the script names it and fetches nothing — never a guessed version, never
    an unverifiable checksum."""
    repo = _repo(tmp_path)
    f = repo / _HADOLINT_SRC
    text = f.read_text(encoding="utf-8")
    line = f'HADOLINT_VERSION = "{_HADOLINT}"\n'
    if damage == "missing":
        text = text.replace(line, "")
    elif damage == "duplicated":
        text = text.replace(line, line + line)
    f.write_text(text, encoding="utf-8")
    if damage == "not-a-version":
        _set_hadolint_pin(repo, HADOLINT_VERSION="latest")
    elif damage == "not-a-sha":
        _set_hadolint_pin(repo, HADOLINT_SHA256="abc123")

    r, marker, calls = _run(tmp_path, repo, None)

    assert r.returncode == 1, r.stdout + r.stderr
    why = f"pin unreadable from {_HADOLINT_SRC}"
    assert f"hadolint=FAILED ({why})" in marker.splitlines(), marker
    assert _result(marker)[0].endswith(f"hadolint: {why})"), marker
    assert _hadolint_downloads(calls["curl"]) == [], calls["curl"]


# ── kube-linter, pint, promtool (#3611) ─────────────────────────────────────
# Same contract as hadolint: the pin is read at run time from the file that
# owns it, installed from its release tarball at that checksum, and a failure
# is RESULT=failed with the reason.

#: tool → (_run keyword, pin source, VERSION name, SHA name, URL fragment for
#: the version, path of the binary inside the release tarball)
_OWNED = {
    "kube-linter": ("kube_linter", _OWNED_PIN_SRCS[0], "KUBE_LINTER_VERSION",
                    "KUBE_LINTER_SHA256", lambda v: f"/download/{v}/kube-linter-linux.tar.gz",
                    lambda v: "kube-linter"),
    "pint": ("pint", _OWNED_PIN_SRCS[1], "PINT_VERSION", "PINT_SHA256",
             lambda v: f"/download/v{v}/pint-{v}-linux-amd64.tar.gz",
             lambda v: "pint-linux-amd64"),
    "promtool": ("promtool", _OWNED_PIN_SRCS[2], "PROM_VERSION", "PROM_SHA256",
                 lambda v: f"/download/v{v}/prometheus-{v}.linux-amd64.tar.gz",
                 lambda v: f"prometheus-{v}.linux-amd64/promtool"),
}
_OWNED_PIN = {"kube-linter": _KUBE_LINTER, "pint": _PINT, "promtool": _PROMTOOL}
_OWNED_REPORT = {"kube-linter": lambda v: v.removeprefix("v"),
                 "pint": lambda v: f"{v} (revision: stub)",
                 "promtool": lambda v: f"promtool, version {v} (branch: stub)"}


def _set_owned_pin(repo: Path, tool: str, name: str, value: str) -> None:
    """Rewrite one pin line in the repo copy of the tool's SSOT file."""
    f = repo / _OWNED[tool][1]
    text = f.read_text(encoding="utf-8")
    if f.suffix == ".py":
        text, n = re.subn(rf'^{name}\s*=\s*"[^"]+"\s*$', f'{name} = "{value}"', text,
                          flags=re.MULTILINE)
    else:
        text, n = re.subn(rf"^{name}=\S+$", f"{name}={value}", text, flags=re.MULTILINE)
    assert n == 1, (tool, name)
    f.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("tool", sorted(_OWNED))
@pytest.mark.parametrize("curl,why", [("fail", "download"), ("garbage", "sha256 mismatch")])
def test_an_owned_pin_tool_that_cannot_be_installed_fails_the_run(tmp_path, tool, curl, why):
    repo = _repo(tmp_path)
    kw, *_ , url, _inner = _OWNED[tool]

    r, marker, calls = _run(tmp_path, repo, None, curl=curl, **{kw: None})

    assert r.returncode == 1, r.stdout + r.stderr
    results = _result(marker)
    assert len(results) == 1 and results[0].endswith(f"{tool}: {why})"), marker
    assert f"{tool}=FAILED ({why})" in marker.splitlines()
    assert any(url(_OWNED_PIN[tool]) in c for c in calls["curl"]), calls["curl"]
    assert not (tmp_path / "tool-bin" / tool).exists()


@pytest.mark.parametrize("tool", sorted(_OWNED))
def test_an_owned_pin_is_read_from_its_ssot_at_run_time(tmp_path, tool):
    """Bump the version in the file that owns it and the script asks for the
    new release; a copy in the script would still fetch the old one."""
    repo = _repo(tmp_path)
    kw, _src, vname, *_rest = _OWNED[tool]
    url = _OWNED[tool][4]
    bumped = "v9.8.7" if tool == "kube-linter" else "9.8.7"
    _set_owned_pin(repo, tool, vname, bumped)

    r, marker, calls = _run(tmp_path, repo, None)  # stand-in reports the real pin

    assert any(url(bumped) in c for c in calls["curl"]), calls["curl"]
    assert f"{tool}=FAILED (download)" in marker.splitlines(), marker


@pytest.mark.parametrize("tool", sorted(_OWNED))
def test_an_owned_pin_tool_is_unpacked_from_its_release_tarball(tmp_path, tool):
    """The binary is taken from its path inside the release tarball, and what
    lands in the install dir is that member, verified by the pinned checksum."""
    import io
    import tarfile
    repo = _repo(tmp_path)
    kw, _src, _vname, sha_name, _url, inner = _OWNED[tool]
    version = _OWNED_PIN[tool]
    binary = f'#!/bin/sh\necho "{_OWNED_REPORT[tool](version)}"\n'.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(inner(version))
        info.size, info.mode = len(binary), 0o755
        tf.addfile(info, io.BytesIO(binary))
    tarball = buf.getvalue()
    _set_owned_pin(repo, tool, sha_name, hashlib.sha256(tarball).hexdigest())

    r, marker, calls = _run(tmp_path, repo, None, curl=tarball, tool_bin_on_path=True,
                            **{kw: None})

    installed = tmp_path / "tool-bin" / tool
    assert installed.read_bytes() == binary, r.stdout + r.stderr
    assert os.access(installed, os.X_OK)
    assert f"{tool}={version}" in marker.splitlines(), marker
    assert all(tool not in line for line in _result(marker)), marker


@pytest.mark.parametrize("tool", sorted(_OWNED))
@pytest.mark.parametrize("damage", ["not-a-version", "not-a-sha"])
def test_an_unreadable_owned_pin_fails_without_downloading(tmp_path, tool, damage):
    repo = _repo(tmp_path)
    kw, _src, vname, sha_name, url, _inner = _OWNED[tool]
    if damage == "not-a-version":
        _set_owned_pin(repo, tool, vname, "latest")
    else:
        _set_owned_pin(repo, tool, sha_name, "deadbeef")

    r, marker, calls = _run(tmp_path, repo, None, **{kw: None})

    assert f"{tool}=FAILED (pin unreadable from {_OWNED[tool][1]})" in marker.splitlines(), marker
    assert not any(url(_OWNED_PIN[tool]) in c for c in calls["curl"]), calls["curl"]


# ── node (#3611) ────────────────────────────────────────────────────────────
# The pin is an exact release in the script (its major bound to the dev
# container by test_toolchain_pin_parity.py); the release is a tree unpacked
# whole, with node / npm / npx linked into a directory ahead of the base
# image's /opt/node22/bin.


def _set_script_pin(repo: Path, name: str, value: str) -> None:
    f = repo / ".claude" / "hooks" / "session-start.sh"
    text, n = re.subn(rf"^{name}=\S+$", f"{name}={value}", f.read_text(encoding="utf-8"),
                      flags=re.MULTILINE)
    assert n == 1, name
    f.write_text(text, encoding="utf-8")


@pytest.mark.parametrize("curl,why", [("fail", "download"), ("garbage", "sha256 mismatch")])
def test_node_that_cannot_be_installed_fails_the_run(tmp_path, curl, why):
    repo = _repo(tmp_path)

    r, marker, calls = _run(tmp_path, repo, None, curl=curl, node=None)

    assert r.returncode == 1, r.stdout + r.stderr
    results = _result(marker)
    assert len(results) == 1 and f"node: {why}" in results[0], marker
    assert f"node=FAILED ({why})" in marker.splitlines()
    assert any(f"/dist/v{_NODE}/node-v{_NODE}-linux-x64.tar.xz" in c
               for c in calls["curl"]), calls["curl"]
    assert not (tmp_path / "node-link-bin" / "node").exists()


def test_node_is_unpacked_whole_and_linked_ahead_on_path(tmp_path):
    """The verified tarball is unpacked into NODE_HOME (npm needs its lib/
    tree), node / npm / npx are linked into the link dir, and the node PATH
    then resolves is the pinned one."""
    import io
    import tarfile
    repo = _repo(tmp_path)
    top = f"node-v{_NODE}-linux-x64"
    members = {f"{top}/bin/node": f'#!/bin/sh\necho "v{_NODE}"\n'.encode(),
               f"{top}/bin/npm": b"#!/bin/sh\nexit 0\n",
               f"{top}/bin/npx": b"#!/bin/sh\nexit 0\n",
               f"{top}/lib/node_modules/npm/package.json": b"{}\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    tarball = buf.getvalue()
    _set_script_pin(repo, "NODE_SHA256", hashlib.sha256(tarball).hexdigest())

    r, marker, calls = _run(tmp_path, repo, None, curl=tarball, tool_bin_on_path=True,
                            node=None)

    home = tmp_path / "node-home" / f"node-v{_NODE}"
    assert (home / "lib" / "node_modules" / "npm" / "package.json").is_file(), \
        r.stdout + r.stderr
    for t in ("node", "npm", "npx"):
        link = tmp_path / "node-link-bin" / t
        assert link.is_symlink() and link.resolve() == (home / "bin" / t).resolve(), t
    assert f"node=v{_NODE}" in marker.splitlines(), marker
    assert all("node" not in line for line in _result(marker)), marker
