"""A dev tool that runs git on its own tree refuses a tree git does not root (#2852).

`_lib_compat.require_git_project_root` takes the tree a tool sits in
(``require_project_root``) and accepts it only when ``git rev-parse
--show-toplevel`` names the same directory. Before it, three tools took "the
nearest `.git` above this file" as their root, so a copy vendored into another
repository ran `git ls-files` / `git log` over the OUTER repository.

Every layout here is built for real — a copy of `scripts/tools` (the shared
libs plus the tool under test) in a tmp dir, with `git init` and a commit where
the layout calls for one — and the tool is run as a subprocess, the way an
operator runs it:

  * vendored: an outer repo tracks the copy as ordinary files → rc 2, and the
    one stderr line names the copy's root and the root git reported;
  * vendored with the outer `GIT_DIR` exported (what a git hook hands its
    children) → still rc 2: with `GIT_DIR` set, `--show-toplevel` reports the
    `-C` directory itself, so a helper that keeps the variable passes;
  * no git anywhere above → rc 2, naming git's own refusal;
  * the copy is its own clone, and a `git worktree` of that clone → it runs.

`_rule_tree` resolves its root on first use, not at import: importing it (as
`_rule_tripwire` does for the pure `_decode_manifest`) needs no git at all.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def _clean_env(**extra: str) -> dict:
    """The test process's environment minus anything that points git
    somewhere (pytest may itself run under a git hook), plus *extra*."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(_GIT_ENV)
    env.update(extra)
    return env


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, timeout=60, env=_clean_env())


def _copy_tree(root: Path, *tool_rels: str) -> Path:
    """`root/scripts/tools` holding the shared libs and *tool_rels*; returns root."""
    dest = root / "scripts" / "tools"
    dest.mkdir(parents=True)
    for lib in TOOLS.glob("*.py"):
        shutil.copy2(lib, dest / lib.name)
    for rel in tool_rels:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(TOOLS / rel, dest / rel)
    # Something for check_pipestatus_errexit to measure.
    (root / "ok.sh").write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    return root


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--no-verify", "-m", "fixture")


def _layout(kind: str, tmp_path: Path, *tool_rels: str) -> "tuple[Path, Path | None]":
    """Build *kind*; return (the copy's root, the outer repo or None)."""
    if kind == "vendored":
        outer = tmp_path / "outer"
        outer.mkdir()
        _git(outer, "init", "-q")
        (outer / "README").write_text("outer\n", encoding="utf-8")
        root = _copy_tree(outer / "vendor" / "dai", *tool_rels)
        _commit_all(outer)
        return root, outer
    if kind == "no-git":
        return _copy_tree(tmp_path / "plain", *tool_rels), None
    clone = _copy_tree(tmp_path / "clone", *tool_rels)
    _git(clone, "init", "-q")
    _commit_all(clone)
    if kind == "clone":
        return clone, None
    assert kind == "worktree", kind
    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "-q", str(wt))
    return wt, None


def _run(root: Path, tool_rel: str, *args: str, **env: str):
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "tools" / tool_rel), *args],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(root),
        env=_clean_env(**env))


def _env_for(kind: str, tmp_path: Path, outer: "Path | None", gitdir: bool) -> dict:
    env = {}
    if kind == "no-git":
        # Nothing above tmp_path may be taken for a repository either.
        env["GIT_CEILING_DIRECTORIES"] = str(tmp_path)
    if gitdir:
        env["GIT_DIR"] = str(outer / ".git")
    return env


def _assert_named_refusal(proc, root: Path, why: str) -> None:
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    err = [ln for ln in proc.stderr.splitlines() if ln.startswith("ERROR:")]
    assert len(err) == 1, proc.stderr
    assert str(root.resolve()) in err[0], err[0]
    assert why in err[0], err[0]
    assert "#2852" in err[0], err[0]


_REFUSED = [
    pytest.param("vendored", False, id="vendored"),
    pytest.param("vendored", True, id="vendored-with-outer-GIT_DIR"),
    pytest.param("no-git", False, id="no-git"),
]


def _why(kind: str, outer: "Path | None") -> str:
    if outer is not None:
        return f"git reports the work tree root {outer.resolve()}"
    return "not a git repository"


# ── scan_component_health: the root is taken at import ──────────────────────

SCH = "dx/scan_component_health.py"


@pytest.mark.parametrize("kind,gitdir", _REFUSED)
def test_scan_component_health_refuses(kind, gitdir, tmp_path):
    root, outer = _layout(kind, tmp_path, SCH)
    proc = _run(root, SCH, "--summary-only",
                **_env_for(kind, tmp_path, outer, gitdir))
    _assert_named_refusal(proc, root, _why(kind, outer))


@pytest.mark.parametrize("kind", ["clone", "worktree"])
def test_scan_component_health_accepts_its_own_tree(kind, tmp_path):
    # `--help` is parsed after the import-time root check, so rc 0 here means
    # the check passed; the full scan needs the registry and portal sources,
    # which the real-repo tests (tests/dx/test_scan_component_health.py) cover.
    root, _ = _layout(kind, tmp_path, SCH)
    proc = _run(root, SCH, "--help")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# ── check_pipestatus_errexit: only the DEFAULT root is checked ──────────────

PIPE = "lint/check_pipestatus_errexit.py"


@pytest.mark.parametrize("kind,gitdir", _REFUSED)
def test_pipestatus_default_root_refuses(kind, gitdir, tmp_path):
    root, outer = _layout(kind, tmp_path, PIPE)
    proc = _run(root, PIPE, **_env_for(kind, tmp_path, outer, gitdir))
    _assert_named_refusal(proc, root, _why(kind, outer))


@pytest.mark.parametrize("kind", ["clone", "worktree"])
def test_pipestatus_default_root_measures_its_own_tree(kind, tmp_path):
    root, _ = _layout(kind, tmp_path, PIPE)
    proc = _run(root, PIPE, "--json")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    # The population is the copy's own tracked shell file and nothing else.
    assert '"population": 1' in proc.stdout, proc.stdout


def test_pipestatus_explicit_root_is_taken_as_given(tmp_path):
    """`--root` never goes through the git check: a vendored copy pointed at a
    tree explicitly measures that tree."""
    root, outer = _layout("vendored", tmp_path, PIPE)
    proc = _run(root, PIPE, "--root", str(outer), "--json")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


# ── _rule_tree: the root is taken on first use ──────────────────────────────

_PROBE = """
import sys
sys.path.insert(0, sys.argv[1])
import _rule_tree
assert _rule_tree._REPO_ROOT is None, _rule_tree._REPO_ROOT
text, enc = _rule_tree._decode_manifest(b"a: 1\\n")
assert (text, enc) == ("a: 1\\n", "utf-8"), (text, enc)
print("IMPORTED-WITHOUT-ROOT", flush=True)
_rule_tree._repo_root()
"""


def test_rule_tree_resolves_its_root_lazily(tmp_path):
    root, outer = _layout("vendored", tmp_path, "lint/_rule_tree.py",
                          "lint/check_rulepack_sync.py")
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, str(root / "scripts" / "tools" / "lint")],
        capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(root),
        env=_clean_env())
    # Import and the pure decoder worked in a vendored copy …
    assert "IMPORTED-WITHOUT-ROOT" in proc.stdout, (proc.stdout, proc.stderr)
    # … and the first read of the tree is where the layout is refused.
    _assert_named_refusal(proc, root, _why("vendored", outer))


def test_rule_tree_resolves_this_checkout(monkeypatch):
    sys.path.insert(0, str(TOOLS / "lint"))
    import _rule_tree  # noqa: PLC0415
    monkeypatch.setattr(_rule_tree, "_REPO_ROOT", None)
    assert os.path.samefile(_rule_tree._repo_root(), REPO)
