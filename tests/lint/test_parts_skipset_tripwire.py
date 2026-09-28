"""Tripwire: a skip-set must not be matched against an ABSOLUTE ``.parts``.

Why this exists (#2314): #1810 / PR #2299 fixed four tools that decided
"is this file under an excluded directory?" by testing ``path.parts`` of the
ABSOLUTE path against a skip-set such as ``{"examples", "tests", ".git"}``.
That lets the checkout location decide: a repository cloned under
``.../examples/repo/`` or built under ``/tmp/build/tests/...`` has *every*
file excluded, and the lint passes after scanning nothing. The fix was to
match the path RELATIVE to the scan root. This test is the class guard that
keeps the shape from coming back in a new tool.

What is flagged (AST scan of ``scripts/**/*.py``). ``X.parts`` — optionally
wrapped in ``set()`` / ``frozenset()`` / ``list()`` / ``tuple()`` /
``sorted()`` or sliced —
used as:

1. the operand of an ``in`` / ``not in`` comparison
   (``"tests" in p.parts``);
2. the receiver or argument of ``.intersection()`` / ``.isdisjoint()`` /
   ``.issubset()`` / ``.issuperset()``;
3. an operand of ``&`` (``SKIP & set(p.parts)``);
4. the iterable of a comprehension whose loop variable is the left operand
   of an ``in`` / ``not in`` test (``any(s in SKIP for s in p.parts)`` — the
   dominant spelling in this repo, and the one #2299 fixed in
   ``check_hardcode_tenant``);

unless ``X`` is provably relative:

* a direct ``.relative_to(...)`` call (``p.relative_to(root).parts``);
* ``.as_posix()`` / ``.parent`` of something provably relative, or
  ``Path()`` / ``PurePath()`` / ``PurePosixPath()`` of it;
* a local name whose EVERY binding in the enclosing function (module, at top
  level) is a plain assignment of something provably relative
  (``rel = p.relative_to(root)`` … ``rel.parts``). A name bound any other way
  — loop target, parameter, augmented assignment, walrus, ``with`` — is not
  resolved and counts as absolute. This one hop exists so that the idiomatic
  FIX is not itself flagged; without it every correct call site would need an
  exception entry.

Anything else found is either a bug or a false positive that goes in
``EXCEPTIONS`` with a reason. ``EXCEPTIONS`` is exit-locked: every entry must
still match a live hit, so an entry whose code was fixed or deleted turns
this test red instead of silently widening the allowlist.

⚠️ Known blind spots — the scan does NOT see these, so "no hit" does not
mean "no absolute-path skip-set test":

* assign-then-compare: ``ps = p.parts`` followed by ``if "x" in ps`` or
  ``for s in ps: if s in SKIP`` (the ``.parts`` node is not inside the test);
* string splitting instead of ``.parts``: ``str(p).split("/")`` /
  ``p.as_posix().split("/")`` / ``os.sep`` splits;
* substring tests on the path string: ``"/tests/" in str(p)``;
* helper indirection: ``any(s in SKIP for s in _segments(p))``;
* a loop body instead of a comprehension (``for s in p.parts: if s in S``);
* files outside ``scripts/`` (``tests/``, ``components/``, Go code).

``test_documented_blind_spots_are_really_blind`` pins these, so this list
cannot claim a blind spot the scanner has quietly learned to see (or vice
versa).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from _tree import REPO_ROOT, repo_files

# ═══════════════════════════════════════════════════════════════════════════
# Exception table — exit-locked
# ═══════════════════════════════════════════════════════════════════════════
# Keyed by (repo-relative file, enclosing function qualname, a substring of
# the flagged expression's source). No line numbers: they drift on every
# unrelated edit above the site, and a drifted line would either go stale
# (red for no reason) or, worse, start excusing a different line.
#
# Value is (exact number of live hits the key must match, reason). The count
# makes the lock two-sided: 0 live hits = stale entry, MORE live hits = a new
# site is hiding behind an old excuse.
EXCEPTIONS: dict[tuple[str, str, str], tuple[int, str]] = {
    ("scripts/tools/lint/_rule_tree.py", "_tracked_yaml_paths",
     "_SCAN_SKIP_PARTS & set(PurePosixPath(p).parts)"): (1,
        "`p` is an entry of `git ls-files -z` output, which git always "
        "prints relative to the repository root (`-C _REPO_ROOT`). The "
        "checkout location never appears in it, so the skip-set cannot "
        "match an ancestor of the repo. Not provable by the one-hop "
        "resolution because `p` is a comprehension variable over a "
        "subprocess string."),
    ("scripts/tools/lint/check_cross_ns_url_consistency.py", "check_repo",
     "for part in Path(rel).parts"): (2,
        "Both sites (helm values loop, k8s manifest loop) test a `rel` "
        "assigned on the line directly above as "
        "`p.relative_to(repo).as_posix()`. The one-hop resolution cannot "
        "prove it because the same function later rebinds `rel` as a "
        "for-loop tuple target over `findings` (the scope model is "
        "flow-insensitive), so the name has a non-plain binding."),}


# ═══════════════════════════════════════════════════════════════════════════
# Scanner
# ═══════════════════════════════════════════════════════════════════════════
_SET_METHODS = frozenset({"intersection", "isdisjoint", "issubset", "issuperset"})
_WRAPPERS = frozenset({"set", "frozenset", "list", "tuple", "sorted"})
_PATH_CTORS = frozenset({"Path", "PurePath", "PurePosixPath", "PureWindowsPath"})


@dataclass(frozen=True)
class Hit:
    file: str
    line: int
    func: str      # enclosing function qualname, "<module>" at top level
    shape: str     # "in", "set-method", "bitand", "comprehension"
    source: str    # source segment of the flagged expression

    def matches(self, key: tuple[str, str, str]) -> bool:
        f, fn, snippet = key
        return self.file == f and self.func == fn and snippet in self.source

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.func}] ({self.shape}) {self.source}"


def _parts_attr(node: ast.expr) -> ast.Attribute | None:
    """The ``X.parts`` node if ``node`` is it (possibly wrapped/sliced)."""
    while True:
        if isinstance(node, ast.Subscript):
            node = node.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in _WRAPPERS and len(node.args) == 1
              and not node.keywords):
            node = node.args[0]
        else:
            break
    if isinstance(node, ast.Attribute) and node.attr == "parts":
        return node
    return None


class _Scope:
    """Name bindings of one function (or the module), for the one-hop rule."""

    def __init__(self, node: ast.AST):
        # name -> list of assigned value exprs; None marks a non-plain binding
        self.bindings: dict[str, list[ast.expr | None]] = {}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs,
                        *([a.vararg] if a.vararg else []),
                        *([a.kwarg] if a.kwarg else [])):
                self.bindings.setdefault(arg.arg, []).append(None)
        body = node.body if isinstance(node.body, list) else [node.body]
        for stmt in body:
            self._collect(stmt)

    def _collect(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            if not isinstance(node, ast.Lambda):
                self.bindings.setdefault(node.name, []).append(None)
            return  # separate scope
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.bindings.setdefault(t.id, []).append(node.value)
                else:
                    self._mark_stores(t)
            self._collect(node.value)
            return
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            self.bindings.setdefault(node.target.id, []).append(node.value)
            if node.value is not None:
                self._collect(node.value)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            self.bindings.setdefault(node.id, []).append(None)
            return
        for child in ast.iter_child_nodes(node):
            self._collect(child)

    def _mark_stores(self, target: ast.AST) -> None:
        for n in ast.walk(target):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                self.bindings.setdefault(n.id, []).append(None)


def _is_relative(node: ast.expr, scope: _Scope, seen: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "relative_to":
            return True
        if node.func.attr == "as_posix" and not node.args:
            return _is_relative(node.func.value, scope, seen)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in _PATH_CTORS and len(node.args) == 1:
            return _is_relative(node.args[0], scope, seen)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return _is_relative(node.value, scope, seen)
    if isinstance(node, ast.Name) and node.id not in seen:
        values = scope.bindings.get(node.id)
        if not values:
            return False  # free / global / builtin name: unknown
        return all(v is not None and _is_relative(v, scope, seen | {node.id})
                   for v in values)
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(self, file: str, src: str, tree: ast.Module):
        self.file, self.src = file, src
        self.hits: list[Hit] = []
        self._stack: list[str] = []
        self._scopes: list[_Scope] = [_Scope(tree)]

    # -- scope tracking --------------------------------------------------
    def _enter(self, node, name: str) -> None:
        self._stack.append(name)
        self._scopes.append(_Scope(node))
        self.generic_visit(node)
        self._scopes.pop()
        self._stack.pop()

    def visit_FunctionDef(self, node):  # noqa: N802
        self._enter(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

    def visit_ClassDef(self, node):  # noqa: N802
        self._stack.append(node.name)
        self.generic_visit(node)
        self._stack.pop()

    # -- helpers ----------------------------------------------------------
    def _flag(self, parts: ast.Attribute | None, anchor: ast.AST, shape: str) -> None:
        if parts is None or _is_relative(parts.value, self._scopes[-1]):
            return
        self.hits.append(Hit(
            file=self.file, line=anchor.lineno,
            func=".".join(self._stack) or "<module>", shape=shape,
            source=ast.get_source_segment(self.src, anchor) or ast.unparse(anchor)))

    # -- shapes -----------------------------------------------------------
    def visit_Compare(self, node):  # noqa: N802
        operands = [node.left, *node.comparators]
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.In, ast.NotIn)):
                self._flag(_parts_attr(operands[i + 1]), node, "in")
        self.generic_visit(node)

    def visit_Call(self, node):  # noqa: N802
        if isinstance(node.func, ast.Attribute) and node.func.attr in _SET_METHODS:
            for cand in (node.func.value, *node.args):
                self._flag(_parts_attr(cand), node, "set-method")
        self.generic_visit(node)

    def visit_BinOp(self, node):  # noqa: N802
        if isinstance(node.op, ast.BitAnd):
            for cand in (node.left, node.right):
                self._flag(_parts_attr(cand), node, "bitand")
        self.generic_visit(node)

    def _visit_comp(self, node) -> None:
        tests = [node.elt] if not isinstance(node, ast.DictComp) else [node.key, node.value]
        for gen in node.generators:
            tests.extend(gen.ifs)
        for gen in node.generators:
            parts = _parts_attr(gen.iter)
            if parts is None or not isinstance(gen.target, ast.Name):
                continue
            var = gen.target.id
            if any(isinstance(c, ast.Compare) and isinstance(c.left, ast.Name)
                   and c.left.id == var
                   and any(isinstance(o, (ast.In, ast.NotIn)) for o in c.ops)
                   for t in tests for c in ast.walk(t)):
                self._flag(parts, node, "comprehension")
        self.generic_visit(node)

    visit_GeneratorExp = visit_ListComp = visit_SetComp = visit_DictComp = _visit_comp  # noqa: N815


def scan_source(src: str, file: str = "<snippet>") -> list[Hit]:
    tree = ast.parse(src)
    v = _Visitor(file, src, tree)
    v.visit(tree)
    return v.hits


def _scripts_py() -> list[Path]:
    root = REPO_ROOT / "scripts"
    return [p for p in repo_files(".py") if root in p.parents]


def scan_repo() -> list[Hit]:
    hits: list[Hit] = []
    for p in _scripts_py():
        rel = p.relative_to(REPO_ROOT).as_posix()
        hits.extend(scan_source(p.read_text(encoding="utf-8"), rel))
    return hits


# ═══════════════════════════════════════════════════════════════════════════
# Live repo
# ═══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def live_hits() -> list[Hit]:
    return scan_repo()


def test_scan_population_is_not_empty():
    """Fail closed: an empty population makes every test below vacuous."""
    files = _scripts_py()
    assert len(files) > 50, f"only {len(files)} scripts/**/*.py found"


def test_no_absolute_parts_against_skip_sets(live_hits):
    unexcused = [h for h in live_hits
                 if not any(h.matches(k) for k in EXCEPTIONS)]
    assert not unexcused, (
        "`.parts` of a path not provably relative to the scan root is being "
        "matched against a set (#1810 / #2314). On an absolute path the "
        "checkout location takes part in the match: a repo under "
        "`/tmp/build/tests/` has every file skipped. Match "
        "`p.relative_to(root).parts` instead, or — if the path is already "
        "relative (e.g. from `git ls-files`) — add an EXCEPTIONS entry with "
        "the reason:\n  " + "\n  ".join(map(str, unexcused)))


def test_every_exception_still_matches_a_live_hit(live_hits):
    live = {k: sum(h.matches(k) for h in live_hits) for k in EXCEPTIONS}
    wrong = {k: (EXCEPTIONS[k][0], n) for k, n in live.items()
             if n != EXCEPTIONS[k][0]}
    assert not wrong, (
        "EXCEPTIONS entries whose live hit count differs from the pinned "
        "count, as {key: (pinned, live)}. live 0 = stale entry, delete it; "
        "live > pinned = a NEW site is hiding behind an old excuse "
        f"(exit-lock): {wrong}")


def test_every_exception_has_a_reason():
    for key, (_, why) in EXCEPTIONS.items():
        assert len(why.strip()) >= 40, f"{key}: reason too thin"


# ═══════════════════════════════════════════════════════════════════════════
# Scanner unit tests
# ═══════════════════════════════════════════════════════════════════════════
_FLAGGED = {
    "in": "def f(p):\n    return 'tests' in p.parts\n",
    "not in": "def f(p):\n    return 'tests' not in p.parts\n",
    "sliced": "def f(p):\n    return 'tests' in p.parts[:-1]\n",
    "intersection": "def f(p):\n    return SKIP.intersection(p.parts)\n",
    "isdisjoint": "def f(p):\n    return SKIP.isdisjoint(p.parts)\n",
    "issubset recv": "def f(p):\n    return set(p.parts).issubset(SKIP)\n",
    "bitand": "def f(p):\n    return SKIP & set(p.parts)\n",
    "bitand rhs": "def f(p):\n    return frozenset(p.parts) & SKIP\n",
    "any-gen": "def f(p):\n    return any(s in SKIP for s in p.parts)\n",
    "gen-if": "def f(p):\n    return [s for s in p.parts if s not in SKIP]\n",
    "#2299 revert": (
        "def _is_excluded_path(path, root=None):\n"
        "    return any(seg in _PATH_SKIP_DIR_SEGMENTS for seg in path.parts)\n"),
    "Path(abs)": "def f(p):\n    return any(s in S for s in Path(str(p)).parts)\n",
    "name from abs": "def f(p):\n    q = p.resolve()\n    return 'x' in q.parts\n",
    "mixed bindings": (
        "def f(p, root):\n    rel = p.relative_to(root)\n    rel = p\n"
        "    return 'x' in rel.parts\n"),
    "loop var": "def f(ps):\n    for rel in ps:\n        if 'x' in rel.parts:\n            pass\n",
    "param named rel": "def f(rel):\n    return 'x' in rel.parts\n",
    "module level": "'x' in P.parts\n",
}

_CLEAN = {
    "direct relative_to": "def f(p, r):\n    return 'x' in p.relative_to(r).parts\n",
    "parent.relative_to": "def f(p, r):\n    return 'x' in p.parent.relative_to(r).parts\n",
    "one-hop name": (
        "def f(p, r):\n    rel = p.relative_to(r)\n"
        "    return any(s in S for s in rel.parts)\n"),
    "Path(as_posix)": (
        "def f(p, r):\n    rel = p.relative_to(r).as_posix()\n"
        "    return any(s in S for s in Path(rel).parts)\n"),
    "rel.parent": (
        "def f(p, r):\n    rel = p.relative_to(r)\n    return 'x' in rel.parent.parts\n"),
    "intersection rel": "def f(p, r):\n    return S.intersection(p.relative_to(r).parts)\n",
    "not a membership test": "def f(p):\n    return len(p.parts) == 1\n",
    "startswith gen": "def f(p):\n    return any(s.startswith('.') for s in p.parts)\n",
    "gen var not tested": "def f(p):\n    return [s.lower() for s in p.parts if FLAG in S]\n",
}


@pytest.mark.parametrize("name", sorted(_FLAGGED))
def test_scanner_flags(name):
    assert scan_source(_FLAGGED[name]), f"{name!r} should be flagged"


@pytest.mark.parametrize("name", sorted(_CLEAN))
def test_scanner_passes(name):
    hits = scan_source(_CLEAN[name])
    assert not hits, f"{name!r} should not be flagged: {hits}"


_BLIND = {
    "assign-then-compare": "def f(p):\n    ps = p.parts\n    return 'x' in ps\n",
    "assign-then-gen": "def f(p):\n    ps = p.parts\n    return any(s in S for s in ps)\n",
    "str.split": "def f(p):\n    return 'tests' in str(p).split('/')\n",
    "as_posix split": "def f(p):\n    return any(s in S for s in p.as_posix().split('/'))\n",
    "substring": "def f(p):\n    return '/tests/' in str(p)\n",
    "helper": "def f(p):\n    return any(s in S for s in _segments(p))\n",
    "loop body": "def f(p):\n    for s in p.parts:\n        if s in S:\n            return True\n",
}


@pytest.mark.parametrize("name", sorted(_BLIND))
def test_documented_blind_spots_are_really_blind(name):
    """If this fails, the scanner learned a shape: move it out of the
    module docstring's blind-spot list and into ``_FLAGGED``."""
    assert not scan_source(_BLIND[name])


def test_hit_carries_function_qualname():
    src = "class C:\n    def m(self, p):\n        return 'x' in p.parts\n"
    (hit,) = scan_source(src, "f.py")
    assert hit.func == "C.m" and hit.shape == "in" and "p.parts" in hit.source
