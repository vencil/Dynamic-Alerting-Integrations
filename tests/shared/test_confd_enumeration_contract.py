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
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"

# A file "reads a tenant config dir" if it mentions one of these knobs.
_DIR_HINTS = ("config_dir", "conf-d", "conf_d")

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


def _classify(path: pathlib.Path):
    """Return (reads_config_dir, flat, recursive, guarded)."""
    src = path.read_text(encoding="utf-8", errors="replace")
    if not any(h in src for h in _DIR_HINTS):
        return False, False, False, False
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return False, False, False, False
    kinds = {_call_kind(n) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return True, "flat" in kinds, "recursive" in kinds, "guard" in kinds


def _readers():
    return sorted(
        (p for p in TOOLS.rglob("*.py") if _classify(p)[0]),
        key=lambda p: p.as_posix(),
    )


def test_at_least_one_reader_is_discovered():
    """Tripwire: if the hint list ever stops matching, the gate goes quiet.

    A gate that discovers zero subjects passes vacuously — the exact
    failure mode this repo has paid for before.
    """
    assert len(_readers()) >= 10, (
        "conf.d reader discovery collapsed — check _DIR_HINTS still matches "
        "how tools name their config-dir argument"
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
        f"nested_yaml_warning` and print it when it returns non-None\n"
        f"See issue #1339."
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
        f"`nested_yaml_warning(...)` call into the scanning function, or make "
        f"that function recurse. See issue #1339."
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
