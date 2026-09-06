"""Every conf.d reader must be recursive OR say it is not (#1339).

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

_RECURSIVE_CALLS = {"rglob", "walk"}
# Every way a caller can list ONE directory level. Enumerated deliberately
# and kept in one place — an enumerated guard is only as good as its list,
# so both analysers below read this same function rather than each keeping
# their own copy (the duplication that let `iterdir` slip through review).
_FLAT_CALLS = {"listdir", "iterdir", "scandir"}
_GUARD_CALLS = {"warn_nested", "nested_yaml_warning", "nested_yaml_files"}


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
    if name == "iter_config_files":
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


def _enumeration_kinds(path: pathlib.Path) -> set[str]:
    """Every `_call_kind` this file's AST contains — {} if unparseable."""
    tree = parse(path)
    if tree is None:
        return set()
    return {k for k in (_call_kind(n) for n in ast.walk(tree)
                        if isinstance(n, ast.Call)) if k}


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
        kinds = {_call_kind(c) for c in _own_scope_calls(fn)}
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
    "dx/gen_agent_adapters.py":
        "os.listdir over agents/skills — the repo-owned skill SSOT it generates adapters from",
    "dx/inject_waveform.py":
        "`--rules` is a PrometheusRule candidate file/dir (glob *.yaml/*.yml), not a tenant conf.d",
    "dx/pr_preflight.py":
        "globs .github/workflows",
    "lint/check_maintenance_symmetry.py":
        "iterdir over rule-packs/",
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
