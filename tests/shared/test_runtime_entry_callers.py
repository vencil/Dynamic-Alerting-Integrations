"""Who may call the runtime entries (#3981, owner 2026-11-28).

The runtime class of ``_lib_output.OUTPUT_ENTRY_POINTS`` — a private
directory a service makes for its own running, and the files in it — is
for the paths a da-tools command declares ``kind: runtime`` in
``caller_path_manifest.yaml``. Nothing here lists the callers: both sides
are derived, and held against each other.

* the commands: every manifest command with a flag declared ``runtime``;
  its script is the command's ``COMMAND_MAP`` entry (``entrypoint.py``,
  read by ``_lint_helpers.parse_command_map``), found under
  ``scripts/tools``.
* the callers: every ``.py`` file of the repository under ``scripts/``
  (``_tree.repo_files``) that calls a runtime entry, or imports one by
  name, other than ``_lib_output`` itself — read statically (AST).

Direction A: a caller is the script of a runtime command, or a module that
script imports by name (``_gsvc_child``, which ``generator_service``
imports). Direction B: every runtime command's script calls a runtime
entry at least once — a command that declares a runtime path and makes
its directory some other way is the gap this closes.

WHAT IT DOES NOT SEE
--------------------
* **Which call writes which path.** A runtime script may call the entry
  for a path no runtime flag names; where each write lands is the
  generator-service control run's measurement
  (``tests/ops/test_gsvc_runtime_writes.py``), not this one's.
* **A module imported further away.** Only what the script itself imports
  is allowed (one level): a module two imports away that starts calling
  the entry is a fault here, on purpose.
* **A call through a variable or ``getattr``**, and code that is not Python.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(TOOLS))

from _tree import repo_files  # noqa: E402

import _lib_output  # noqa: E402

MANIFEST = Path(__file__).with_name("caller_path_manifest.yaml")
ENTRY_MODULE = "_lib_output"


def runtime_entries() -> frozenset[str]:
    """The runtime entries' function names, read off the library."""
    return frozenset(name.split(".", 1)[1]
                     for name in _lib_output.OUTPUT_ENTRY_POINTS["runtime"])


def reaches(tree: ast.Module, entries: frozenset[str]) -> tuple[bool, bool]:
    """(imports a runtime entry by name, calls one) for one module: a call
    of a name imported from ``_lib_output`` (under any alias), or of
    ``<alias of _lib_output>.<entry>``."""
    names: set[str] = set()
    modules: set[str] = set()
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == ENTRY_MODULE and not node.level:
            for alias in node.names:
                if alias.name in entries:
                    names.add(alias.asname or alias.name)
                    imported = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ENTRY_MODULE:
                    modules.add(alias.asname or alias.name)
    called = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in names:
            called = True
        elif (isinstance(f, ast.Attribute) and f.attr in entries
              and isinstance(f.value, ast.Name) and f.value.id in modules):
            called = True
    return imported, called


def calling_functions(tree: ast.Module, entries: frozenset[str]) -> frozenset[str]:
    """The name of the innermost function around every call of a runtime
    entry in *tree* (spelled as `reaches` sees one) — the frame a write the
    call makes carries on its stack. The generator-service control run is
    held to reaching each of them."""
    names = {alias.asname or alias.name
             for node in ast.walk(tree)
             if isinstance(node, ast.ImportFrom) and node.module == ENTRY_MODULE
             and not node.level
             for alias in node.names if alias.name in entries}
    modules = {alias.asname or alias.name for node in ast.walk(tree)
               if isinstance(node, ast.Import) for alias in node.names
               if alias.name == ENTRY_MODULE}
    found: set[str] = set()

    def visit(node: ast.AST, where: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else where
            if isinstance(child, ast.Call):
                f = child.func
                if ((isinstance(f, ast.Name) and f.id in names)
                        or (isinstance(f, ast.Attribute) and f.attr in entries
                            and isinstance(f.value, ast.Name) and f.value.id in modules)):
                    found.add(where)
            visit(child, inner)

    visit(tree, "<module>")
    return frozenset(found)


def callers() -> dict[str, bool]:
    """Repo-relative file → whether it CALLS a runtime entry, for every file
    under ``scripts/`` that imports or calls one (``_lib_output`` itself
    left out: it is where they live)."""
    entries = runtime_entries()
    found: dict[str, bool] = {}
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if not rel.startswith("scripts/") or rel == f"scripts/tools/{ENTRY_MODULE}.py":
            continue
        imported, called = reaches(ast.parse(path.read_text(encoding="utf-8"), rel), entries)
        if imported or called:
            found[rel] = called
    return found


def runtime_commands(manifest: dict) -> list[str]:
    """Every command of *manifest* with a flag declared ``kind: runtime``."""
    return sorted(cmd for cmd, flags in (manifest.get("commands") or {}).items()
                  if any(isinstance(d, dict) and d.get("kind") == "runtime"
                         for d in (flags or {}).values()))


def command_scripts() -> dict[str, str]:
    """da-tools command → its script, repo-relative: ``COMMAND_MAP`` gives
    the file name, the one file of that name under ``scripts/tools``."""
    sys.path.insert(0, str(TOOLS / "lint"))
    try:
        from _lint_helpers import parse_command_map
    finally:
        sys.path.remove(str(TOOLS / "lint"))
    by_name: dict[str, list[str]] = {}
    for path in repo_files(".py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith("scripts/tools/"):
            by_name.setdefault(path.name, []).append(rel)
    out = {}
    for command, script in parse_command_map().items():
        where = by_name.get(script, [])
        if len(where) == 1:
            out[command] = where[0]
    return out


def imported_modules(script: str) -> frozenset[str]:
    """The repo files *script* imports by name (``import X`` / ``from X
    import …``, absolute), found the way its ``sys.path`` finds them: beside
    it first, then in ``scripts/tools``."""
    tree = ast.parse((REPO_ROOT / script).read_text(encoding="utf-8"), script)
    heads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            heads.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            heads.add(node.module.split(".")[0])
    here = (REPO_ROOT / script).parent
    found = set()
    for head in heads:
        for folder in (here, TOOLS):
            candidate = folder / f"{head}.py"
            if candidate.is_file():
                found.add(candidate.relative_to(REPO_ROOT).as_posix())
                break
    return frozenset(found)


def verdict(calls: dict[str, bool], commands: dict[str, str],
            imports: dict[str, frozenset[str]]) -> list[str]:
    """Every fault of the two directions. *calls*: `callers()`; *commands*:
    runtime command → script; *imports*: script → `imported_modules`."""
    faults = []
    allowed = {script for script in commands.values()}
    for script in commands.values():
        allowed |= imports.get(script, frozenset())
    for rel in sorted(set(calls) - allowed):
        faults.append(f"{rel} reaches a runtime entry and is neither the script of a "
                      "command with a `kind: runtime` flag nor a module one imports: "
                      "declare the path `runtime` in caller_path_manifest.yaml, or write "
                      "it through the operator / artifact entry")
    for command, script in sorted(commands.items()):
        if not calls.get(script):
            faults.append(f"`{command}` declares a runtime path and {script} calls no "
                          "runtime entry: its runtime directory is made some other way")
    return faults


# ── The gate ───────────────────────────────────────────────────────────────

def _runtime_scripts() -> dict[str, str]:
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    scripts = command_scripts()
    commands = runtime_commands(manifest)
    missing = [c for c in commands if c not in scripts]
    assert not missing, f"runtime command(s) with no one script in COMMAND_MAP: {missing}"
    return {c: scripts[c] for c in commands}


def test_the_runtime_entries_are_called_only_where_a_runtime_path_is_declared():
    commands = _runtime_scripts()
    assert commands, "no command declares a runtime path: this gate holds nothing"
    calls = callers()
    assert calls, "no caller of a runtime entry found: the scan measured nothing"
    imports = {script: imported_modules(script) for script in commands.values()}
    faults = verdict(calls, commands, imports)
    assert not faults, "\n  ".join(["the runtime entries and the manifest disagree:", *faults])


def test_the_measured_population_is_the_one_this_change_made():
    """Anti-vacuity on the real tree: the one runtime command today, its
    script, and the module it hands the judgement to, are what the scan
    finds — a scanner that lost the attribute arm, or a command map read
    wrong, would find less."""
    commands = _runtime_scripts()
    assert commands == {"generator-service": "scripts/tools/ops/generator_service.py"}
    assert "scripts/tools/ops/_gsvc_child.py" in imported_modules(
        "scripts/tools/ops/generator_service.py")
    assert callers() == {"scripts/tools/ops/generator_service.py": True,
                         "scripts/tools/ops/_gsvc_child.py": True}


# ── Controls: the scanner and the verdict on hand-made input ───────────────

_ENTRIES = frozenset({"make_runtime_dir", "write_runtime_file"})


@pytest.mark.parametrize("source,want", [
    ("from _lib_output import make_runtime_dir\nmake_runtime_dir(p)\n", (True, True)),
    ("from _lib_output import make_runtime_dir as m\nm(p)\n", (True, True)),
    ("import _lib_output\n_lib_output.write_runtime_file(p, b'')\n", (False, True)),
    ("import _lib_output as o\no.write_runtime_file(p, b'')\n", (False, True)),
    ("from _lib_output import make_runtime_dir\n", (True, False)),
    # not the entry: another module's function of that name, an operator entry
    ("import other\nother.make_runtime_dir(p)\n", (False, False)),
    ("from _lib_output import ensure_dir\nensure_dir(p)\n", (False, False)),
    ("def make_runtime_dir(p): pass\nmake_runtime_dir(p)\n", (False, False)),
])
def test_the_scanner_sees_a_call_under_any_spelling(source, want):
    assert reaches(ast.parse(source), _ENTRIES) == want


def test_the_calling_function_is_the_innermost_def():
    source = ("from _lib_output import make_runtime_dir as m\n"
              "import _lib_output as o\n"
              "m(top)\n"
              "class S:\n"
              "    def job(self):\n"
              "        def inner():\n"
              "            o.write_runtime_file(p, b'')\n"
              "        m(p)\n"
              "def other():\n"
              "    print(p)\n")
    assert calling_functions(ast.parse(source), _ENTRIES) == {"<module>", "job", "inner"}


def test_the_verdict_says_no_in_both_directions():
    cmds = {"svc": "scripts/tools/ops/svc.py"}
    imports = {"scripts/tools/ops/svc.py": frozenset({"scripts/tools/ops/_child.py"})}
    ok = {"scripts/tools/ops/svc.py": True, "scripts/tools/ops/_child.py": True}
    assert verdict(ok, cmds, imports) == []
    # A: a module neither the script nor one it imports
    stray = {**ok, "scripts/tools/dx/other.py": True}
    assert len(verdict(stray, cmds, imports)) == 1
    # A: only importing the name is reaching it too
    assert len(verdict({**ok, "scripts/tools/dx/other.py": False}, cmds, imports)) == 1
    # B: the runtime script makes no call (an import only, or nothing)
    assert len(verdict({"scripts/tools/ops/_child.py": True}, cmds, imports)) == 1
    assert len(verdict({**ok, "scripts/tools/ops/svc.py": False}, cmds, imports)) == 1
    # no runtime command at all: every caller is a stray
    assert len(verdict(ok, {}, {})) == 2


def test_the_runtime_commands_are_read_off_the_flag_kinds():
    manifest = {"commands": {
        "a": {"--d": {"kind": "runtime", "by": "x"}, "--n": "not_path"},
        "b": {"--o": "output", "--r": {"kind": "reader"}},
        "c": None,
    }}
    assert runtime_commands(manifest) == ["a"]


def test_imported_modules_are_found_beside_the_script_then_in_tools():
    found = imported_modules("scripts/tools/ops/generator_service.py")
    assert "scripts/tools/ops/_gsvc_proto.py" in found        # beside it
    assert "scripts/tools/_lib_output.py" in found             # scripts/tools
    assert not any(rel.endswith("/os.py") for rel in found)   # the stdlib is not a repo file
