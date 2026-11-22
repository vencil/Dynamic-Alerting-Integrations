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
  * the copy is its own clone, and a `git worktree` of that clone → it runs;
  * the copy is its own clone INSIDE an outer repo whose `GIT_DIR` /
    `GIT_INDEX_FILE` (or a `GIT_WORK_TREE`) is inherited → rc 2: the root
    check passes, but every later git of the tool would read the outer repo;
  * this tree's OWN commit hooks — main checkout, `commit -a` (index.lock),
    linked worktree — run the tool with git's real hook variables → rc 0;
  * a clone owned by another uid, with `safe.directory` reaching git through
    `GIT_CONFIG_COUNT` → rc 0 (the `GIT_CONFIG*` carriers are not dropped).

The helper's own failure edges (a variable list without GIT_DIR, a git that
cannot run) are driven in-process with `subprocess.run` replaced.

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

from _platform_fs import symlink_or_skip  # noqa: E402

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


# ── inherited git variables must belong to this tree ────────────────────────

def _own_clone_inside_outer(tmp_path: Path, *tool_rels: str) -> "tuple[Path, Path]":
    """The copy is its OWN clone, sitting inside an outer repository."""
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    (outer / "README").write_text("outer\n", encoding="utf-8")
    (outer / "outer.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _commit_all(outer)
    root = _copy_tree(outer / "vendor" / "dai", *tool_rels)
    _git(root, "init", "-q")
    _commit_all(root)
    return root, outer


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"])
@pytest.mark.parametrize("tool", [PIPE, SCH])
def test_inherited_variable_of_an_outer_repo_is_refused(tool, var, tmp_path):
    """An outer repo's hook exports its GIT_DIR / GIT_INDEX_FILE (a
    GIT_WORK_TREE may be set too). The root check alone passes — the copy IS
    its own clone — but every later git of the tool would read the outer repo."""
    root, outer = _own_clone_inside_outer(tmp_path, tool)
    value = {"GIT_DIR": outer / ".git", "GIT_INDEX_FILE": outer / ".git" / "index",
             "GIT_WORK_TREE": outer}[var]
    proc = _run(root, tool, "--json" if tool == PIPE else "--help",
                **{var: str(value)})
    _assert_named_refusal(proc, root, f"inherited {var}={value}")


def test_dot_dot_after_a_symlink_is_resolved_as_git_opens_it(tmp_path):
    """`<root>/lnk/../.git/index` reads as this tree's index once `..` is
    folded away literally, but `lnk` points into the outer repo, so git opens
    the OUTER index. The check must resolve the path on disk, as git does."""
    root, outer = _own_clone_inside_outer(tmp_path, PIPE)
    (outer / "sub").mkdir()
    symlink_or_skip(outer / "sub", root / "lnk")
    value = f"{root}/lnk/../.git/index"
    proc = _run(root, PIPE, "--json", GIT_INDEX_FILE=value)
    _assert_named_refusal(proc, root, f"inherited GIT_INDEX_FILE={value}")


def test_a_relative_git_dir_is_taken_against_the_root(tmp_path):
    """The tools run git with `-C <root>` / `cwd=<root>`, where a relative
    GIT_DIR resolves against the root — not against the caller's cwd."""
    root, _ = _layout("clone", tmp_path, PIPE)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "tools" / PIPE), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
        cwd=str(elsewhere), env=_clean_env(GIT_DIR=".git"))
    assert proc.returncode == 0, (proc.returncode, proc.stderr)


def test_a_gitfile_git_dir_is_compared_by_where_it_points(tmp_path):
    """A linked worktree's `.git` is a gitfile git accepts as GIT_DIR: this
    tree's own is accepted, one pointing at another repository is refused."""
    wt, _ = _layout("worktree", tmp_path, PIPE)
    assert (wt / ".git").is_file()
    own = _run(wt, PIPE, "--json", GIT_DIR=str(wt / ".git"))
    assert own.returncode == 0, (own.returncode, own.stderr)

    (tmp_path / "foreign").mkdir()
    root, outer = _own_clone_inside_outer(tmp_path / "foreign", PIPE)
    gitfile = tmp_path / "foreign.gitfile"
    gitfile.write_text(f"gitdir: {outer / '.git'}\n", encoding="utf-8")
    proc = _run(root, PIPE, "--json", GIT_DIR=str(gitfile))
    _assert_named_refusal(proc, root, f"inherited GIT_DIR={gitfile}")


_HOOK = """#!/bin/sh
"{py}" "$PWD/scripts/tools/{tool}" --json > /dev/null 2> "{log}.err"
echo $? > "{log}.rc"
env | grep '^GIT_' > "{log}.env"
exit 0
"""


def _hook_run(repo: Path, hook_dir: Path, tmp_path: Path, name: str, *commit_args):
    """`git commit` in *repo* with a real pre-commit hook that runs the tool;
    returns (the tool's rc, the GIT_* env the hook got, the tool's stderr)."""
    log = tmp_path / name
    hook = hook_dir / "pre-commit"
    hook.write_text(_HOOK.format(py=sys.executable, tool=PIPE, log=log),
                    encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", *commit_args],
                   check=True, capture_output=True, timeout=120, env=_clean_env())
    read = lambda ext: (tmp_path / f"{name}.{ext}").read_text(encoding="utf-8")  # noqa: E731
    return read("rc").strip(), read("env"), read("err")


def test_this_trees_own_commit_hooks_still_pass(tmp_path):
    """The variables git hands this repo's OWN hooks are this tree's: a commit
    in the main checkout, `commit -a` (GIT_INDEX_FILE = index.lock), and a
    commit in a linked worktree (GIT_DIR = .git/worktrees/<name>)."""
    clone, _ = _layout("clone", tmp_path, PIPE)
    hooks = clone / ".git" / "hooks"

    (clone / "new.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(clone, "add", "new.sh")
    rc, env, err = _hook_run(clone, hooks, tmp_path, "main", "-m", "main")
    assert rc == "0", (env, err)

    (clone / "ok.sh").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    rc, env, err = _hook_run(clone, hooks, tmp_path, "all", "-a", "-m", "all")
    assert rc == "0", (env, err)
    assert "GIT_INDEX_FILE=" in env and "index.lock" in env, env  # the case is real

    wt = tmp_path / "wt"
    _git(clone, "worktree", "add", "-q", str(wt))
    (wt / "wt.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(wt, "add", "wt.sh")
    rc, env, err = _hook_run(wt, hooks, tmp_path, "wt", "-m", "wt")
    assert rc == "0", (env, err)
    assert "/worktrees/" in env, env  # the hook got the linked worktree's GIT_DIR


# ── the GIT_CONFIG* carriers are kept ───────────────────────────────────────

@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="needs root to give the clone another owner")
def test_clone_owned_by_another_user_with_safe_directory_from_env(tmp_path):
    """A clone owned by another uid is "dubious ownership" unless
    safe.directory reaches git — here through GIT_CONFIG_COUNT, which the
    helper must not drop along with the location variables."""
    clone, _ = _layout("clone", tmp_path, PIPE)
    for dirpath, dirnames, filenames in os.walk(clone):
        os.lchown(dirpath, 54321, 54321)
        for name in dirnames + filenames:
            os.lchown(os.path.join(dirpath, name), 54321, 54321)
    bare = _run(clone, PIPE, "--json")
    assert bare.returncode == 2 and "dubious ownership" in bare.stderr, bare.stderr
    proc = _run(clone, PIPE, "--json", GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="safe.directory", GIT_CONFIG_VALUE_0="*")
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def _helper_with_fake_run(monkeypatch, tmp_path, fake):
    """The helper on a copied tree, in-process, with `subprocess.run` → *fake*."""
    sys.path.insert(0, str(TOOLS))
    import _lib_compat  # noqa: PLC0415
    tree = _copy_tree(tmp_path / "t")
    # The helper imports subprocess locally, so patch the module itself.
    monkeypatch.setattr(subprocess, "run", fake)
    return lambda: _lib_compat.require_git_project_root(tree / "scripts" / "tools")


def test_config_carriers_survive_and_location_variables_do_not(monkeypatch, tmp_path):
    """Measured without needing another uid: the env of the root question."""
    seen = {}
    real = subprocess.run

    def fake(argv, **kw):
        if kw.get("env") is not None:  # the root question: the env it asks with
            seen.update(kw["env"])
        return real(argv, **kw)

    for k, v in {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                 "GIT_CONFIG_VALUE_0": "*", "GIT_CONFIG_PARAMETERS": "'a.b'='c'",
                 "GIT_CONFIG": os.devnull, "GIT_DIR": str(tmp_path / "nowhere"),
                 "GIT_INDEX_FILE": str(tmp_path / "nowhere" / "index")}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    call = _helper_with_fake_run(monkeypatch, tmp_path, fake)
    with pytest.raises(SystemExit):
        call()  # no git tree here; only the env of the question matters
    for kept in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG"):
        assert kept in seen, kept
    assert "GIT_DIR" not in seen and "GIT_INDEX_FILE" not in seen, seen


@pytest.mark.parametrize("stdout", [b"", b"GIT_WORK_TREE\n"])
def test_a_variable_list_without_git_dir_fails_closed(monkeypatch, tmp_path, capsys, stdout):
    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout, b"")
    call = _helper_with_fake_run(monkeypatch, tmp_path, fake)
    with pytest.raises(SystemExit) as exc:
        call()
    assert exc.value.code == 2
    assert "did not list GIT_DIR" in capsys.readouterr().err


@pytest.mark.parametrize("error", [FileNotFoundError(2, "No such file"),
                                   PermissionError(13, "Permission denied")])
def test_git_that_cannot_run_is_a_named_refusal(monkeypatch, tmp_path, capsys, error):
    def fake(argv, **kw):
        raise error
    call = _helper_with_fake_run(monkeypatch, tmp_path, fake)
    with pytest.raises(SystemExit) as exc:
        call()
    assert exc.value.code == 2
    assert "cannot run git" in capsys.readouterr().err


# ── the same decisions, in-process (#2852 follow-up) ─────────────────────────
# The end-to-end cells above run each tool in a subprocess, which coverage
# does not see. These call the helper itself on the same real layouts, so
# every branch of require_git_project_root is measured where it runs.

def _helper_in_process(monkeypatch, **env: str):
    """The repo's helper, called in this process with *env* as the only git
    variables (the test may itself run under a git hook)."""
    sys.path.insert(0, str(TOOLS))
    import _lib_compat  # noqa: PLC0415
    for k in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(k)
    for k, v in {**_GIT_ENV, **env}.items():
        monkeypatch.setenv(k, v)
    return _lib_compat.require_git_project_root


def _refused(capsys, call, *args) -> str:
    with pytest.raises(SystemExit) as exc:
        call(*args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ERROR:") and "(#2852)" in err, err
    return err


@pytest.mark.parametrize("kind", ["clone", "worktree"])
def test_in_process_own_tree_is_returned(kind, tmp_path, monkeypatch):
    root, _ = _layout(kind, tmp_path)
    helper = _helper_in_process(monkeypatch)
    assert helper(root / "scripts" / "tools") == root


def test_in_process_another_work_tree_root_is_refused(tmp_path, monkeypatch, capsys):
    root, outer = _layout("vendored", tmp_path)
    helper = _helper_in_process(monkeypatch)
    err = _refused(capsys, helper, root / "scripts" / "tools")
    assert f"git reports the work tree root {outer}" in err


def test_in_process_relative_and_gitfile_git_dir_are_accepted(tmp_path, monkeypatch):
    clone, _ = _layout("clone", tmp_path / "c")
    assert _helper_in_process(monkeypatch, GIT_DIR=".git")(
        clone / "scripts" / "tools") == clone
    wt, _ = _layout("worktree", tmp_path / "w")
    assert (wt / ".git").is_file()
    assert _helper_in_process(monkeypatch, GIT_DIR=str(wt / ".git"))(
        wt / "scripts" / "tools") == wt


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"])
def test_in_process_foreign_inherited_variable_is_refused(var, tmp_path, monkeypatch, capsys):
    root, outer = _own_clone_inside_outer(tmp_path)
    value = {"GIT_DIR": outer / ".git", "GIT_INDEX_FILE": outer / ".git" / "index",
             "GIT_WORK_TREE": outer}[var]
    helper = _helper_in_process(monkeypatch, **{var: str(value)})
    err = _refused(capsys, helper, root / "scripts" / "tools")
    assert f"inherited {var}={value}" in err


def test_in_process_gitfile_pointing_elsewhere_is_refused(tmp_path, monkeypatch, capsys):
    root, outer = _own_clone_inside_outer(tmp_path)
    gitfile = tmp_path / "foreign.gitfile"
    gitfile.write_text(f"gitdir: {outer / '.git'}\n", encoding="utf-8")
    helper = _helper_in_process(monkeypatch, GIT_DIR=str(gitfile))
    err = _refused(capsys, helper, root / "scripts" / "tools")
    assert f"inherited GIT_DIR={gitfile}" in err


def test_a_root_that_cannot_be_compared_is_a_named_refusal(monkeypatch, tmp_path, capsys):
    """git answers with a toplevel that does not exist: samefile raises,
    which must be a named refusal, not a traceback."""
    def fake(argv, **kw):
        out = b"GIT_DIR\n" if "--local-env-vars" in argv else b"/no/such/toplevel\n"
        return subprocess.CompletedProcess(argv, 0, out, b"")
    call = _helper_with_fake_run(monkeypatch, tmp_path, fake)
    err = _refused(capsys, call)
    assert "git's work tree root '/no/such/toplevel' cannot be compared" in err


def test_git_that_hangs_is_a_named_refusal(monkeypatch, tmp_path, capsys):
    def fake(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
    call = _helper_with_fake_run(monkeypatch, tmp_path, fake)
    assert "timed out" in _refused(capsys, call)
