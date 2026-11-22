"""No Python file finds "its repo" by asking git or by walking up to a `.git` (#2852).

Both shapes answer "which repository encloses this directory", which is the
wrong question for a tool reading its OWN tree: copied into another
repository, the answer is the outer one, and every `git ls-files` / `git log`
that follows reads someone else's files. Such a tool uses
`_lib_compat.require_git_project_root` (or `require_project_root` when it runs
no git), the one place allowed to ask git.

The two shapes, found by AST in every Python file of the tree
(`tests/_tree.repo_files`):

  * WALK — an existence probe of ``<x> / ".git"`` (or ``os.path.join(<x>,
    ".git")``, ``<p>.joinpath(".git")``, ``Path(<x>, ".git")``) —
    ``.exists()`` / ``.is_dir()`` / ``.is_file()`` /
    ``os.path.exists|isdir|isfile|lexists`` — inside a loop or comprehension:
    the walk up the parents. A probe of ``root / ".git" / "hooks"`` is not
    one (the ``.git`` is not the probed path), nor is a single probe outside
    any loop.
  * TOPLEVEL — a string constant containing ``--show-toplevel`` that is not a
    docstring: a git argv built to ask the same question.

Some files mean to ask it: a tool whose object is the work tree the CALLER is
in (the cwd), or the repository a CUSTOMER's output directory sits in. Those
are listed in `ALLOWED`, one reason per file and shape. The list is checked
both ways: an entry whose file is gone or no longer shows that shape is red
too, so the list cannot outlive what it excuses.

Known blind spots (the detector reads constants, not values): a ``.git``
spelled by string concatenation (``d + "/.git"``), an f-string, a ``.git``
held in a variable or constant, a probe through a helper function, or a
``--show-toplevel`` assembled from pieces are not seen.

⛔ Shell scripts are out of scope: this ratchet reads Python's AST only, and a
`git rev-parse --show-toplevel` in a `.sh` / `.bat` / workflow is not seen.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _tree import REPO_ROOT, repo_files  # noqa: E402

WALK, TOPLEVEL = "walk-up-to-.git", "--show-toplevel"

#: The one module allowed to ask git where the work tree begins.
HELPER = "scripts/tools/_lib_compat.py"

_CWD = ("its object is the work tree the caller runs it in (the cwd), "
        "not the tree the file sits in")

ALLOWED: dict[tuple[str, str], str] = {
    # ── the caller's work tree ──
    ("scripts/tools/dx/pr_preflight.py", WALK): _CWD,
    ("scripts/tools/lint/check_pr_scope_drift.py", WALK): _CWD,
    ("scripts/tools/lint/check_ad_hoc_git_scripts.py", WALK): _CWD,
    ("scripts/tools/lint/check_bat_ascii_purity.py", WALK): _CWD,
    ("scripts/tools/dx/generate_changelog.py", TOPLEVEL): _CWD,
    ("scripts/ops/fuse_plumbing_commit.py", TOPLEVEL):
        "commits in the work tree the caller is in; the FUSE escape hatch "
        "runs from wherever the stuck checkout is",
    # ── Claude Code session guards: the project the session opened ──
    ("scripts/session-guards/paths_map.py", WALK):
        "maps paths in the session's project (CLAUDE_PROJECT_DIR / cwd), "
        "which is what a hook is asked about",
    ("scripts/session-guards/session-init.py", WALK):
        "walks up from its own file (cwd only as a fallback) to the checkout "
        "it is installed in, whose .git/hooks it heals. It cannot use the "
        "helper: it runs under the hook protocol, where exit 2 BLOCKS the "
        "tool call, and the helper refuses with SystemExit(2). It runs no "
        "git ls-files / git log over the tree",
    ("scripts/session-guards/vscode_git_toggle.py", WALK):
        "toggles VS Code's git integration for the opened workspace",
    # ── a customer's repository ──
    ("scripts/tools/dx/migrate_conf_d.py", TOPLEVEL):
        "asks which repository the CUSTOMER's conf.d sits in, to plan git mv",
    ("scripts/tools/ops/init_project.py", WALK):
        "asks whether the customer's -o output dir is below their repo root, "
        "to refuse certifying CI wiring written one level down",
    ("scripts/tools/ops/migrate_credentials.py", WALK):
        "asks which git worktree the customer's --manifests-out dir is in",
    # ── tests that measure git itself ──
    ("tests/ops/test_prepush_hook_wiring.py", TOPLEVEL):
        "measures what git reports for a --git-dir push, and writes a user "
        "hook (shell text) that uses it",
    ("tests/shared/test_no_nearest_git_root.py", TOPLEVEL):
        "this ratchet's own detector fixtures (source text, never run)",
}

_PROBES = {"exists", "is_dir", "is_file", "lexists", "isdir", "isfile"}
_LOOPS = (ast.For, ast.AsyncFor, ast.While, ast.ListComp, ast.SetComp,
          ast.DictComp, ast.GeneratorExp)


def _is_dot_git(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == ".git"


_PATH_CTORS = {"Path", "PurePath", "PosixPath", "PurePosixPath",
               "WindowsPath", "PureWindowsPath"}


def _is_git_path(node: ast.AST) -> bool:
    """A path whose LAST component is the constant `.git`:
    `<x> / ".git"`, `os.path.join(..., ".git")`, `<p>.joinpath(..., ".git")`,
    `Path(<x>, ".git")` (and the other pathlib constructors)."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_dot_git(node.right)
    if not (isinstance(node, ast.Call) and node.args):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else None)
    if name in ("join", "joinpath"):
        return _is_dot_git(node.args[-1])
    if name in _PATH_CTORS:
        return len(node.args) >= 2 and _is_dot_git(node.args[-1])
    return False


def _is_git_probe(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in _PROBES):
        return False
    # `(<x> / ".git").exists()` or `os.path.isdir(os.path.join(<x>, ".git"))`
    return _is_git_path(node.func.value) or bool(
        node.args and _is_git_path(node.args[0]))


def shapes(source: str | bytes) -> set[str]:
    tree = ast.parse(source)
    found = set()
    for loop in ast.walk(tree):
        if isinstance(loop, _LOOPS) and any(
                _is_git_probe(n) for n in ast.walk(loop)):
            found.add(WALK)
            break
    docstrings = {id(n.value) for n in ast.walk(tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "--show-toplevel" in n.value and id(n) not in docstrings
           for n in ast.walk(tree)):
        found.add(TOPLEVEL)
    return found


def _measure() -> dict[tuple[str, str], None]:
    hits = {}
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            found = shapes(path.read_bytes())
        except SyntaxError as exc:  # a file this ratchet cannot read is red
            raise AssertionError(f"{rel}: cannot parse ({exc})") from exc
        for shape in found:
            hits[(rel, shape)] = None
    return hits


@pytest.fixture(scope="module")
def hits():
    measured = _measure()
    # Fail-closed: the scan saw the helper's own `--show-toplevel`, so it
    # read the tree rather than nothing.
    assert (HELPER, TOPLEVEL) in measured, "the scan did not see the helper"
    return measured


def test_no_unlisted_file_finds_its_repo_root_by_git(hits):
    unlisted = sorted(k for k in hits if k[0] != HELPER and k not in ALLOWED)
    assert not unlisted, (
        "these files find the repository root by walking up to `.git` or by "
        "`git rev-parse --show-toplevel`; vendored into another repo they read "
        "the OUTER one (#2852). Use _lib_compat.require_git_project_root, or "
        "list the file in ALLOWED with why it means to ask about another "
        f"tree:\n" + "\n".join(f"  {p}: {s}" for p, s in unlisted))


def test_every_allowed_entry_still_applies(hits):
    stale = sorted(k for k in ALLOWED if k not in hits)
    assert not stale, (
        "ALLOWED excuses a shape these files no longer have (or the file is "
        "gone); remove the entry:\n" + "\n".join(f"  {p}: {s}" for p, s in stale))


def test_every_allowed_entry_gives_a_reason():
    assert all(why.strip() for why in ALLOWED.values())
    assert all(shape in (WALK, TOPLEVEL) for _, shape in ALLOWED)


# ── the detector itself ──

@pytest.mark.parametrize("src", [
    # the three tools' removed forms (#2852)
    "for p in [start, *start.parents]:\n    if (p / '.git').exists():\n        x = p\n",
    "for c in here.parents:\n    if (c / '.git').is_dir():\n        pass\n",
    "while True:\n    if os.path.lexists(os.path.join(d, '.git')):\n        break\n",
    "r = next(p for p in here.parents if (p / '.git').exists())\n",
    "for c in here.parents:\n    if Path(c, '.git').exists():\n        pass\n",
    "for c in here.parents:\n    if c.joinpath('.git').is_dir():\n        pass\n",
    "for c in here.parents:\n    if pathlib.PurePath(c, '.git').exists():\n        pass\n",
    "subprocess.run(['git', 'rev-parse', '--show-toplevel'])\n",
    "cmd = ['git', '-C', d, 'rev-parse', '--show-toplevel']\n",
])
def test_detector_sees_the_shapes(src):
    assert shapes(src), src


@pytest.mark.parametrize("src", [
    # a path under .git is not a root probe (session-init / pr_preflight)
    "for h in names:\n    if (repo_root / '.git' / 'hooks' / h).exists():\n        pass\n",
    "for h in names:\n    if root.joinpath('.git', 'hooks', h).exists():\n        pass\n",
    "for h in names:\n    if Path('.git').exists():\n        pass\n",
    # one probe outside any loop is not a walk
    "if (root / '.git').exists():\n    pass\n",
    # prose about the flag is not a call
    "def f():\n    '''uses git rev-parse --show-toplevel'''\n",
])
def test_detector_ignores_lookalikes(src):
    assert not shapes(src), src
