"""Every conf.d reader must be recursive OR say it is not (PR #1343; conf.d family #1911).

ADR-016 allows a hierarchical `conf.d/` and threshold-exporter walks it
(`pkg/config/hierarchy.go`, `filepath.WalkDir`). The Python suite did not:
at the time this gate was written **11 tools enumerated a tenant config
dir flat while 6 recursed**, so two tools pointed at the same directory
disagreed about whether it contained any tenants. `validate_config.py` was
the worst — `Result: PASS` / exit 0 while scanning **0 tenants**.

The defect is not "flat" (some tools are flat by design). The defect is
**flat AND silent**: reporting nothing looks exactly like finding nothing.

So the contract is a choice, and this gate makes it explicit:

  (a) enumerate recursively — `_lib_confd.iter_config_files`, `rglob`,
      or `os.walk`; or
  (b) stay flat but call `_lib_confd.nested_yaml_warning(...)`, which
      names the files being skipped.

A NEW tool that reads a config dir flat and says nothing fails here. That
is the point: the silent-zero class is closed, not merely documented.

Derivation, not enumeration: membership is computed from what the file
actually calls, so a tool cannot dodge the gate by spelling its flat scan
differently — a new flat form still has to satisfy (a) or (b).

⛔ WHO IS IN THE POPULATION is the other half of that promise (#1761).
The first version decided it by substring — `config_dir` / `conf-d` /
`conf_d` — so a tool that named its knob `dir_path` (`gitops_check`) or
`--dirs` (`drift_detect`) enumerated a conf.d flat and was never
parametrized: not skipped, not red, simply absent. Membership is now the
union of three independently derived keys (`_confd_population`), and the
reconciliation pin at the bottom asks the complementary question — what
enumerates a directory flat and is in NONE of them — so a fourth naming
convention has to be reviewed instead of slipping by.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from _confd_population import parse, population_keys

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

# `list_config_tree` (#2054) is `_lib_confd`'s one recursive walk; unlike
# `iter_config_files` it has no `recursive=` knob, so its name settles it.
# #3206: the walk under it is da-guard's per-path index — a `--list-only`
# run (`_lib_parse_tree.list_tree`, `_lib_confd.config_listing`) or the
# entries of a tree-mode document a reader holds (`ConfdReader.listing`):
# all of them the exporter's recursive walk, so a caller of any of them is
# recursive — unless it keeps only the top level (`_LISTING_CALLS`).
# (`_listing` is `_lib_confd`'s own "the caller's snapshot, else
# config_listing" helper.)
_RECURSIVE_CALLS = {"rglob", "walk", "list_config_tree",
                    "list_tree", "config_listing", "listing", "listing_of", "_listing"}
# Every way a caller can list ONE directory level. Enumerated deliberately
# and kept in one place — an enumerated guard is only as good as its list,
# so both analysers below read this same function rather than each keeping
# their own copy (the duplication that let `iterdir` slip through review).
_FLAT_CALLS = {"listdir", "iterdir", "scandir"}
_GUARD_CALLS = {"warn_nested", "nested_yaml_warning", "nested_yaml_files"}
# Calls whose answer is da-guard's RECURSIVE listing, so a `depth == 0`
# filter over it in the same scope is a flat read (#3206).
_LISTING_CALLS = {"list_tree", "config_listing", "listing", "listing_of", "_listing",
                  "list_config_tree"}


def _call_kind(node: ast.Call) -> str | None:
    """Classify one call as 'flat', 'recursive', 'guard', or None."""
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
    if name in _GUARD_CALLS:
        return "guard"
    if name in _RECURSIVE_CALLS:
        return "recursive"
    if name in _FLAT_CALLS:
        return "flat"
    if name in ("iter_config_files", "unusable_config_paths"):
        # The shared helper has a `recursive=` knob, so the call name alone
        # does not settle it: `iter_config_files(d, recursive=False)` is a
        # flat read wearing a recursive-looking name.
        for kw in node.keywords:
            if kw.arg == "recursive":
                if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    return "recursive"
                return "flat"  # False, or computed → cannot be trusted as recursive
        return "recursive"  # default is recursive=True
    if name == "glob" and node.args and isinstance(node.args[0], ast.Constant):
        pat = node.args[0].value
        # `glob("*.yaml")` is flat; `glob("**/*.yaml")` is not.
        if isinstance(pat, str) and pat.startswith("*.y"):
            return "flat"
    return None


def _own_scope_calls(fn: ast.AST):
    """Yield Calls in THIS function's own scope, not nested definitions.

    `ast.walk` descends into nested `def` / `async def` / `lambda` /
    `class` bodies, so an *uncalled* inner helper that invokes the guard
    would make its enclosing flat scan look guarded while nothing runs at
    runtime. Stop at every nested lexical scope.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def _is_depth0(node: ast.AST) -> bool:
    """`<x>.depth == 0` (either side), `<x>.depth < 1` or `<x>.depth <= 0` —
    keeping only the top level of a da-guard listing (#3206)."""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1):
        return False
    left, op, right = node.left, node.ops[0], node.comparators[0]
    if isinstance(right, ast.Attribute) and isinstance(left, ast.Constant):
        left, right = right, left
        op = {ast.Lt: ast.Gt(), ast.Gt: ast.Lt(), ast.LtE: ast.GtE(),
              ast.GtE: ast.LtE()}.get(type(op), op)
    if not (isinstance(left, ast.Attribute) and left.attr == "depth"
            and isinstance(right, ast.Constant) and isinstance(right.value, int)):
        return False
    v = right.value
    return ((isinstance(op, ast.Eq) and v == 0) or (isinstance(op, ast.Lt) and v == 1)
            or (isinstance(op, ast.LtE) and v == 0))


def _knobs(scope: ast.AST) -> "set[str]":
    """The parameter names of a function scope (none for a module)."""
    a = getattr(scope, "args", None)
    if a is None:
        return set()
    return {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)}


def _flat_listing_filters(scope: ast.AST) -> bool:
    """A `depth == 0` filter in this scope that is not the caller's knob.

    ⛔ (#3206) Taking the top level of da-guard's RECURSIVE listing is a flat
    read wearing a recursive call's name, like `iter_config_files(d,
    recursive=False)`. The one exemption: an `or` whose every other operand
    is a bare NAME that is a parameter of this function (`recursive or
    e.depth == 0`) — a library's own `recursive=` knob, whose flat use is
    caught at the call site. ⛔ Only a parameter: an attribute
    (`e.depth == 0 or e.not_a_file`) or a local is no knob, and the filter
    counts as flat — the first version exempted any attribute, so a
    predicate passed as a knob.

    ⚠️ A TEXT-LEVEL HEURISTIC over one scope's AST, not a dataflow
    analysis. Known shapes it does NOT see (deliberately not chased one by
    one): the depth read through a variable (`d = e.depth; if d == 0`) or a
    helper, a negation (`not e.depth`, `e.depth != 0` in an `else`),
    `min(depth) == 0`, a `filter(lambda e: …)` (a lambda is its own scope),
    slicing a list sorted by depth, and a filter on `path.count("/") == 0`
    or `"/" not in e.path` instead of `depth`. A flat reader written that way
    passes this gate; review is the backstop."""
    knobs = _knobs(scope)
    stack: list[ast.AST] = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for k, v in enumerate(node.values):
                others = node.values[:k] + node.values[k + 1:]
                if _is_depth0(v) and not all(isinstance(o, ast.Name) and o.id in knobs
                                             for o in others):
                    return True
                if not _is_depth0(v):
                    stack.append(v)
            continue
        if _is_depth0(node):
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _name_of(node: ast.Call) -> str:
    fn = node.func
    return fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")


def _scope_kinds(scope: ast.AST, calls) -> set[str]:
    """The kinds of one scope's calls, with a flat listing filter turning the
    listing call it narrows from recursive into flat."""
    calls = list(calls)
    kinds = {_call_kind(c) for c in calls}
    if _flat_listing_filters(scope):
        kinds.add("flat")
        others = [c for c in calls if _call_kind(c) == "recursive"
                  and _name_of(c) not in _LISTING_CALLS]
        if not others:
            kinds.discard("recursive")
    return {k for k in kinds if k}


#: Functions whose top-level filter is the QUESTION, not a flat read of the
#: tree — reviewed one by one, with why nothing below the root bears on it.
_ROOT_ONLY_BY_DEFINITION: dict[tuple[str, str], str] = {
    ("_lib_key_verdict.py", "root_platform_files"):
        "asks whether the conf.d ROOT holds any platform (`_`) file — the "
        "`undecidable` switch of the key verdict (ADR-038); a `_` file below "
        "the root is no root platform file, so there is nothing to skip",
    ("_lib_parse_tree.py", "root_platform_file"):
        "a predicate on ONE entry, not a listing: whether that entry is a "
        "root platform file (Go's isRootPlatformKey, #2743) — a `_` file "
        "below the root is not one by definition (no plane reads its "
        "`tenants:` block, #1576), so nothing is skipped",
}


def _enumeration_kinds(path: pathlib.Path) -> set[str]:
    """Every `_call_kind` this file's AST contains — {} if unparseable —
    plus "flat" for a top-level filter over da-guard's listing (#3206)."""
    tree = parse(path)
    if tree is None:
        return set()
    kinds = {k for k in (_call_kind(n) for n in ast.walk(tree)
                         if isinstance(n, ast.Call)) if k}
    if any(_flat_listing_filters(fn) for fn in ast.walk(tree)
           if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
           and (path.name, fn.name) not in _ROOT_ONLY_BY_DEFINITION):
        kinds.add("flat")
    return kinds


def _classify(path: pathlib.Path):
    """Return (reads_config_dir, flat, recursive, guarded).

    `reads_config_dir` is the union population from `_confd_population`
    (imports `_lib_confd` / declares a conf.d flag / mentions a conf.d
    hint), not a single substring net — see the module docstring.
    """
    if not population_keys(path):
        return False, False, False, False
    kinds = _enumeration_kinds(path)
    return True, "flat" in kinds, "recursive" in kinds, "guard" in kinds


def _readers():
    return sorted(
        (p for p in TOOLS.rglob("*.py") if _classify(p)[0]),
        key=lambda p: p.as_posix(),
    )


def test_at_least_one_reader_is_discovered():
    """Tripwire: if every population key stops matching, the gate goes quiet.

    A gate that discovers zero subjects passes vacuously — the exact
    failure mode this repo has paid for before. The floor reads the UNION
    (`_confd_population.population_keys`), so it only collapses when all
    three keys do at once.
    """
    assert len(_readers()) >= 10, (
        "conf.d reader discovery collapsed — check `_confd_population`'s "
        "three keys (import of _lib_confd / CONFD_FLAGS / CONFD_TEXT_HINTS) "
        "still match how tools take their config dir"
    )


def _flat_scopes_without_guard(path: pathlib.Path) -> list[str]:
    """Functions that enumerate flat but do NOT call the guard themselves.

    Presence of the import somewhere in the file is not enough: a guard
    that never runs on the path that produces the wrong answer is exactly
    the "gate exists but never fires" shape this repo has paid for. Same
    enclosing function is a cheap, derivable reachability proxy.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    bad = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (path.name, fn.name) in _ROOT_ONLY_BY_DEFINITION:
            continue
        kinds = _scope_kinds(fn, _own_scope_calls(fn))
        # Recursing is the other valid answer, so it counts as guarded.
        if "flat" in kinds and not ({"guard", "recursive"} & kinds):
            bad.append(fn.name)
    return bad


@pytest.mark.parametrize("path", _readers(), ids=lambda p: p.name)
def test_flat_confd_reader_is_not_silent(path: pathlib.Path):
    _, flat, recursive, guarded = _classify(path)
    if not flat:
        return  # recursive or no direct enumeration — nothing to prove
    assert recursive or guarded, (
        f"{path.relative_to(REPO).as_posix()} enumerates a conf.d FLAT and says "
        f"nothing about it.\n"
        f"On a hierarchical conf.d (ADR-016 — threshold-exporter reads it "
        f"recursively) this reports zero tenants, which is indistinguishable "
        f"from a config that genuinely has none.\n"
        f"Pick one:\n"
        f"  (a) recurse  — `from _lib_confd import iter_config_files`\n"
        f"  (b) stay flat but speak up — `from _lib_confd import "
        f"warn_nested` and call it in the scanning function; it prints the "
        f"skipped files to stderr once per directory\n"
        f"The full contract is `_lib_confd`'s module docstring."
    )


@pytest.mark.parametrize("path", _readers(), ids=lambda p: p.name)
def test_guard_lives_where_the_flat_scan_lives(path: pathlib.Path):
    """The guard must sit in the SAME function as the flat enumeration.

    Counterfactual for this assertion: moving any tool's
    `nested_yaml_warning(...)` call out of its scanning function (e.g. up
    to module scope) turns this red while the presence check above stays
    green — which is precisely the gap it exists to cover.
    """
    _, flat, _, _ = _classify(path)
    if not flat:
        return
    unguarded = _flat_scopes_without_guard(path)
    assert not unguarded, (
        f"{path.relative_to(REPO).as_posix()}: function(s) {unguarded} enumerate "
        f"a conf.d flat with no guard in the same scope. The import alone does "
        f"not help the code path that produces the wrong answer — move the "
        f"`warn_nested(...)` call into the scanning function, or make that "
        f"function recurse. The full contract is `_lib_confd`'s module "
        f"docstring."
    )


# ── The gate's own bypasses (CodeRabbit review on PR #1343) ─────────────
#
# An enumerated guard is only as good as its list, so the ways around it
# are pinned here as counter-examples. Each of these DID slip through the
# first version of this gate — measured, not imagined.

_BYPASS_CASES = {
    "iterdir": "def scan(config_dir):\n    for p in config_dir.iterdir():\n        pass\n",
    "scandir": "import os\ndef scan(config_dir):\n    for p in os.scandir(config_dir):\n        pass\n",
    # Our own helper has a `recursive=` knob: the call name looks recursive
    # while the read is flat.
    "iter_config_files_recursive_false": (
        "from _lib_confd import iter_config_files\n"
        "def scan(config_dir):\n"
        "    for p in iter_config_files(config_dir, recursive=False):\n        pass\n"
    ),
    # #3206: the top level of da-guard's recursive listing is a flat read;
    # without a warning it must turn the gate red.
    "listing_depth0_without_warning": (
        "from _lib_confd import config_listing\n"
        "def scan(d):\n"
        "    for e in config_listing(d).entries:\n"
        "        if e.depth == 0:\n            pass\n"
    ),
    "reader_listing_depth0_without_warning": (
        "import _lib_parse_cache\n"
        "def scan(d):\n"
        "    return [e for e in _lib_parse_cache.reader_for(d).listing().entries\n"
        "            if e.depth == 0]\n"
    ),
    # An `or` whose other operand is a predicate, not a knob, is a flat
    # read too — an attribute included (the first exemption let
    # `e.depth == 0 or e.not_a_file` through).
    "listing_depth0_or_predicate": (
        "from _lib_confd import config_listing\n"
        "def scan(d):\n"
        "    return [e for e in config_listing(d).entries\n"
        "            if e.depth == 0 or e.cls == 'directory']\n"
    ),
    "listing_depth0_or_attribute": (
        "from _lib_confd import config_listing\n"
        "def scan(d):\n"
        "    return [e for e in config_listing(d).entries\n"
        "            if e.depth == 0 or e.not_a_file]\n"
    ),
    # A bare name that is not this function's parameter is no knob either.
    "listing_depth0_or_local_name": (
        "from _lib_confd import config_listing\n"
        "def scan(d):\n"
        "    everything = False\n"
        "    return [e for e in config_listing(d).entries\n"
        "            if everything or e.depth == 0]\n"
    ),
    "listing_depth0_comprehension": (
        "from _lib_parse_tree import list_tree\n"
        "def scan(d):\n"
        "    return [e for e in list_tree(d).entries if 0 == e.depth]\n"
    ),
    "unusable_config_paths_recursive_false": (
        "from _lib_confd import unusable_config_paths\n"
        "def scan(d):\n"
        "    return unusable_config_paths(d, recursive=False)\n"
    ),
    # A guard inside an inner function that nobody calls emits nothing at
    # runtime, but `ast.walk` used to count it for the enclosing scope.
    "guard_in_uncalled_inner_function": (
        "from _lib_confd import nested_yaml_warning\nimport os\n"
        "def scan(config_dir):\n"
        "    def _never():\n        return nested_yaml_warning(config_dir, tool='x')\n"
        "    for f in os.listdir(config_dir):\n        pass\n"
    ),
}

_ACCEPTED_CASES = {
    "iter_config_files_default_is_recursive": (
        "from _lib_confd import iter_config_files\n"
        "def scan(config_dir):\n    for p in iter_config_files(config_dir):\n        pass\n"
    ),
    "flat_with_guard_in_same_scope": (
        "from _lib_confd import nested_yaml_warning\nimport os\n"
        "def scan(config_dir):\n"
        "    w = nested_yaml_warning(config_dir, tool='x')\n"
        "    for f in os.listdir(config_dir):\n        pass\n"
    ),
    "listing_depth0_with_warning": (
        "from _lib_confd import config_listing, warn_nested\n"
        "def scan(d):\n"
        "    listing = config_listing(d)\n"
        "    warn_nested(d, listing=listing)\n"
        "    return [e for e in listing.entries if e.depth == 0]\n"
    ),
    # The library's own knob: flat only when the caller asks for it, which
    # the call site's `recursive=False` answers.
    "listing_recursive_knob": (
        "from _lib_confd import config_listing\n"
        "def scan(d, recursive=True):\n"
        "    return [e for e in config_listing(d).entries if recursive or e.depth == 0]\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_BYPASS_CASES))
def test_known_bypasses_are_caught(name, tmp_path: pathlib.Path):
    f = tmp_path / f"{name}.py"
    f.write_text(_BYPASS_CASES[name], encoding="utf-8")
    _, flat, recursive, guarded = _classify(f)
    unguarded = _flat_scopes_without_guard(f)
    assert (flat and not (recursive or guarded)) or unguarded, (
        f"{name} slipped through: flat={flat} recursive={recursive} "
        f"guarded={guarded} unguarded_scopes={unguarded}"
    )


@pytest.mark.parametrize("name", sorted(_ACCEPTED_CASES))
def test_legitimate_shapes_are_not_flagged(name, tmp_path: pathlib.Path):
    """The other half: the gate must not cry wolf on correct code.

    Without this, tightening the classifier could pass every bypass test
    by simply rejecting everything.
    """
    f = tmp_path / f"{name}.py"
    f.write_text(_ACCEPTED_CASES[name], encoding="utf-8")
    _, flat, recursive, guarded = _classify(f)
    assert recursive or guarded
    assert _flat_scopes_without_guard(f) == []


# ── Who is in the population (#1761) ──────────────────────────────────
#
# Everything above only speaks about files `_classify` admits. The pin
# below is the complementary question: of every file under scripts/tools
# whose AST enumerates a directory FLAT, which ones are in NONE of the
# population keys? Each must be a repo-owned-tree reader, named with its
# reason, and the assertion is set equality in BOTH directions:
#
#   * a new flat enumerator outside the population lands in the actual
#     set and is not pinned → red → a human decides whether it reads a
#     tenant conf.d (add a key / a hint) or a repo tree (add it here);
#   * a pinned file that starts importing `_lib_confd` (or grows a conf.d
#     flag) drops out of the actual set while still pinned → red → its
#     entry is deleted, because it is now inside the gate proper.
#
# ⚠️ FLAT only, on purpose. Recursive-only enumerators outside the
# population (48 doc/lint tools using `rglob` over docs/, helm/, .github/
# on the tree this was written against) can never turn the gate red —
# the assertions above return early when nothing is flat — so pinning
# them would be a 50-line list that reddens on every new `rglob` doc lint
# and buys no detection. A recursive tool that later adds a flat scan
# enters this set at that moment, which is when the question matters.
#
# ⛔ Set equality has no anti-vacuity of its own (`set() == set()` is
# green). The independent witness is `test_population_keys_synthetic_
# controls` below: a synthetic file built OUTSIDE scripts/tools must land
# in the "flat and outside" set, proving the classifier still produces
# that answer — the pin cannot be satisfied by the classifier going dark.

FLAT_OUTSIDE_POPULATION: dict[str, str] = {
    "_lib_toolcount.py":
        "iterdir over scripts/tools and its ops/dx/lint subdirectories — the "
        "repo's own Python tools, counted case-insensitively (issue 1541)",
    "dx/gen_agent_adapters.py":
        "os.listdir over .agents/skills — the repo-owned skill SSOT it generates adapters from",
    "dx/gen_recipe_contract.py":
        "globs rule-packs/recipes/*.yaml — the repo-owned recipe governance "
        "documents it writes status/enum/default into (issue 3229)",
    "dx/inject_waveform.py":
        "`--rules` is a PrometheusRule candidate file/dir (glob *.yaml/*.yml), not a tenant conf.d",
    "dx/pr_preflight.py":
        "globs .github/workflows",
    "lint/check_image_caveats.py":
        "iterdir over .github/workflows — the repo's own CI workflows, for "
        "image-caveat markers in their comments (issue 1420)",
    "lint/check_chart_ship_surface.py":
        "iterdir over the root of each helm/<chart> the release pipeline "
        "packages — a repo-owned tree, never a tenant conf.d (#1755)",
    "lint/check_maintenance_symmetry.py":
        "iterdir over rule-packs/",
    "lint/check_metric_dictionary.py":
        "globs RULE_PACKS_DIR (rule-packs/) for the alert names and threshold "
        "readers the dictionary contract checks (issue 1196)",
    "lint/check_orphan_lint.py":
        "globs .github/workflows",
    "lint/check_unpinned_deps.py":
        "globs .github/workflows",
    "lint/check_vmalert_coverage.py":
        "iterdir over rule-packs/ and tests/rulepacks/",
    "lint/check_workflow_git_push_permissions.py":
        "globs `--workflows-dir` (.github/workflows)",
    "ops/inject_metadata_join.py":
        "globs RULE_PACKS_DIR (rule-packs/)",
}


def _flat_enumerators_outside_population(root: pathlib.Path) -> set[str]:
    """Files under `root` that enumerate flat and carry no population key."""
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "flat" in _enumeration_kinds(p) and not population_keys(p)
    }


def test_every_flat_enumerator_outside_the_population_is_named():
    """A flat scan the population cannot see must be a reviewed exemption.

    This is what #1761 lacked: `gitops_check` and `drift_detect` both
    enumerated flat, both were outside the substring net, and nothing
    said so. Two-way equality: arriving here unpinned is red, and staying
    pinned after joining the population is red too.
    """
    actual = _flat_enumerators_outside_population(TOOLS)
    pinned = set(FLAT_OUTSIDE_POPULATION)
    assert actual == pinned, (
        f"flat enumerators outside the conf.d population changed.\n"
        f"  unpinned (new flat scan the gate cannot see): "
        f"{sorted(actual - pinned)}\n"
        f"  stale pin (now inside the population — delete): "
        f"{sorted(pinned - actual)}\n"
        f"For a new entry decide which it is: a tenant conf.d reader must "
        f"import `_lib_confd` (or take a `_confd_population.CONFD_FLAGS` "
        f"flag) so the gate proper sees it; a repo-owned-tree reader is "
        f"pinned in FLAT_OUTSIDE_POPULATION with the tree it reads."
    )


# Paired controls (D-05d): each key must admit a file ON ITS OWN, and a
# file with none of them must be classified outside. A synthetic file
# never passes through scripts/tools, so it is the witness that does not
# shrink together with the thing it protects.
_CONTROL_CASES: dict[str, tuple[str, set[str]]] = {
    # The #1761 shape itself: a `--dirs` knob, a flat scan, no hint text,
    # no `_lib_confd` import. MUST be outside — this is what the pin
    # catches, and nothing else in this file would.
    "dirs_knob_no_hint_no_import": (
        "import argparse, os\n"
        "def main():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--dirs')\n"
        "    for d in p.parse_args().dirs.split(','):\n"
        "        for f in os.listdir(d):\n            pass\n",
        set(),
    ),
    # K1 alone: the import binds the module; nothing else mentions conf.d.
    "import_only": (
        "from _lib_confd import warn_nested\n"
        "def scan(d):\n"
        "    warn_nested(d)\n"
        "    for f in d.iterdir():\n        pass\n",
        {"import"},
    ),
    # K2 alone: `--confd` is a CONFD_FLAGS member that contains none of the
    # text hints (`conf-d` / `conf_d` / `conf.d` / `config_dir`).
    "flag_only": (
        "import argparse, os\n"
        "def main():\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--confd')\n"
        "    for f in os.listdir(p.parse_args().confd):\n        pass\n",
        {"flag"},
    ),
    # K3 alone: the literal directory name in a docstring, as
    # `drift_detect`'s usage line has it.
    "hint_only": (
        '"""Usage: tool --dirs cluster-a/conf.d,cluster-b/conf.d"""\n'
        "import os\n"
        "def scan(d):\n"
        "    for f in os.listdir(d):\n        pass\n",
        {"hint"},
    ),
    # A comment naming the library is NOT an import: K1 is an AST key.
    "comment_mentions_library_is_not_a_key": (
        "import os\n"
        "# see _lib_confd for the shared predicates\n"
        "def scan(d):\n"
        "    for f in os.listdir(d):\n        pass\n",
        set(),
    ),
}


@pytest.mark.parametrize("name", sorted(_CONTROL_CASES))
def test_population_keys_synthetic_controls(name, tmp_path: pathlib.Path):
    """Each population key admits on its own; no key ⇒ outside, and the pin sees it.

    The first case is the negative control for the reconciliation pin: a
    flat reader spelled the way `drift_detect` is spelled must show up in
    `_flat_enumerators_outside_population`, otherwise the pin's set
    equality could hold because the classifier stopped answering.
    """
    src, expected = _CONTROL_CASES[name]
    f = tmp_path / f"{name}.py"
    f.write_text(src, encoding="utf-8")
    assert population_keys(f) == expected
    outside = _flat_enumerators_outside_population(tmp_path)
    if expected:
        assert f.name not in outside, "a keyed file must be inside the population"
    else:
        assert f.name in outside, (
            "a flat reader with no population key must surface in the "
            "reconciliation set — that is the only place it can turn red"
        )


# ── `warn_nested` without the caller's snapshot (#3206) ─────────────────
#
# `warn_nested` takes its nested files from the caller's snapshot
# (`listing=` / `reader=`) and raises TypeError with neither — at run time,
# on the path that reaches it. This pin finds such a call before any path
# runs it.

def _warn_nested_without_snapshot(root: pathlib.Path) -> set[str]:
    out = set()
    for p in root.rglob("*.py"):
        if p.name == "_lib_confd.py":
            continue
        tree = parse(p)
        if tree is None:
            continue
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and _name_of(n) == "warn_nested"
                    and not {"listing", "reader"} & {k.arg for k in n.keywords}):
                out.add(p.relative_to(root).as_posix())
    return out


def test_no_tool_calls_warn_nested_without_a_snapshot():
    """Every `warn_nested` call passes the caller's snapshot: the nested
    files it names come from the listing the caller reads, never from a
    second walk (pass it `listing=` / `reader=`)."""
    assert _warn_nested_without_snapshot(TOOLS) == set()


def test_warn_nested_without_a_snapshot_is_detected(tmp_path: pathlib.Path):
    """Anti-vacuity for the pin above: a synthetic caller without a snapshot
    is found, one with `listing=` or `reader=` is not."""
    (tmp_path / "bare.py").write_text(
        "from _lib_confd import warn_nested\ndef f(d):\n    warn_nested(d, tool='x')\n",
        encoding="utf-8")
    (tmp_path / "held.py").write_text(
        "from _lib_confd import warn_nested\n"
        "def f(d, l, r):\n    warn_nested(d, listing=l)\n    warn_nested(d, reader=r)\n",
        encoding="utf-8")
    assert _warn_nested_without_snapshot(tmp_path) == {"bare.py"}
