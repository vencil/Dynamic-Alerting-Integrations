"""#3276 G1: every value a da-tools command takes is declared, path or not.

The shared judgement of a caller-given path (`_lib_paths`) only helps where a
tool calls it, and nothing structural made a new flag call it. This gate walks
every da-tools parser (`check_cli_contract.introspect_parsers`, the walk the
CLI-contract lint already does — not a second one) and requires each argument
that takes a value to be declared in ``caller_path_manifest.yaml`` as one of:

* ``dir`` / ``file`` / ``any`` — judged by `_lib_paths`. It must be RUN: a row
  in ``test_caller_paths.py``'s OPS_SITES / DX_SITES, or a ``no_row`` naming
  the test that pins it where a locked parent cannot reach the check.
* ``reader`` — an input whose own reader judges it with a named error (a
  policy file's ``policy_unusable``, conf.d through da-guard). ``pinned_by``
  names the test.
* ``output`` — a path the tool writes (the write entry's business, #3107).
* ``not_path`` — not a filesystem path (URLs, names, numbers, refs).
* ``pending`` — a caller-given input path not judged yet. It carries the
  ``ticket`` that migrates it, and the set may only shrink: ``PENDING`` below
  is the ceiling, so a new pending entry is a visible edit here, not a
  quiet line in the manifest.

A declaration the parser no longer has is a fault too: the manifest may not
outlive the flag it describes.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
MANIFEST = pathlib.Path(__file__).with_name("caller_path_manifest.yaml")

KINDS = {"dir", "file", "any", "reader", "output", "not_path", "pending"}
JUDGED = {"dir", "file", "any"}
#: The `--json` reason prefix each judged kind answers with (`_lib_paths`).
REASON_PREFIX = {"dir": "dir_", "file": "file_", "any": "path_"}

#: ⛔ The ceiling of `pending`: remove an entry when its flag is migrated; an
#: addition here is a new unjudged input path and needs its own ticket first.
PENDING: frozenset[tuple[str, str]] = frozenset()


def _caller_paths():
    """test_caller_paths.py, for its OPS_SITES / DX_SITES tables."""
    spec = importlib.util.spec_from_file_location(
        "_caller_paths_tables", pathlib.Path(__file__).with_name("test_caller_paths.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rows_by_site() -> dict[tuple[str, str | None], str | None]:
    """(script relative to scripts/tools, flag or None) -> the row's `--json`
    reason (None when the row asserts the message only)."""
    tables = _caller_paths()
    rows: dict[tuple[str, str | None], str | None] = {}
    for _id, script, _argv, flag, reason in tables.OPS_SITES:
        rows[(f"ops/{script}", flag)] = reason
    for _id, script, _argv, named, _rc in tables.DX_SITES:
        first = named.split()[0]
        rows[(script, first if first.startswith("--") else None)] = None
    return rows


def parsed_arguments() -> tuple[dict[tuple[str, str], str], list[str]]:
    """(command, argument) -> script relative to scripts/tools, for every
    argument that takes a value; plus the commands no parser was reached for."""
    import argparse
    sys.path[:0] = [str(TOOLS / "lint"), str(TOOLS)]
    try:
        import check_cli_contract as cli
    finally:
        del sys.path[:2]
    captured, unscoreable, blind, faults = cli.introspect_parsers()
    assert not blind and not faults, (blind, faults)
    command_map = cli.parse_command_map()
    found: dict[tuple[str, str], str] = {}

    def walk(command: str, parser: argparse.ArgumentParser, script: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(f"{command} {name}", sub, script)
                continue
            if action.nargs == 0 or isinstance(action, argparse._HelpAction):
                continue
            longs = [o for o in action.option_strings if o.startswith("--")]
            name = longs[0] if longs else (action.option_strings[0]
                                            if action.option_strings else action.dest)
            found[(command, name)] = script

    for command, model in captured.items():
        script = cli._resolve(command_map[command]).relative_to(TOOLS).as_posix()
        walk(command, model.parser, script)
    return found, sorted(u.split()[0] for u in unscoreable)


def load_manifest(path: pathlib.Path = MANIFEST) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entries(manifest: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for command, args in (manifest.get("commands") or {}).items():
        for arg, decl in (args or {}).items():
            out[(command, arg)] = decl if isinstance(decl, dict) else {"kind": decl}
    return out


def _test_exists(ref: str) -> bool:
    """`tests/x.py::test_name` names a test function that is there."""
    file, _, name = ref.partition("::")
    path = REPO / file
    if not name or not path.is_file():
        return False
    return re.search(rf"^\s*def {re.escape(name)}\(", path.read_text(encoding="utf-8"),
                     re.M) is not None


def manifest_faults(parsed: dict[tuple[str, str], str], dispatched: list[str],
                    manifest: dict, rows: dict, pending_ceiling: frozenset) -> list[str]:
    """Every way the manifest and the parsers disagree, one line each."""
    faults: list[str] = []
    entries = _entries(manifest)
    if sorted(manifest.get("dispatched_to_go") or []) != dispatched:
        faults.append(f"dispatched_to_go is {sorted(manifest.get('dispatched_to_go') or [])}; "
                      f"no Python parser is reached for {dispatched}")
    for key in sorted(set(parsed) - set(entries)):
        faults.append(f"{key[0]} {key[1]}: takes a value and is not declared")
    for key in sorted(set(entries) - set(parsed)):
        faults.append(f"{key[0]} {key[1]}: declared, but the parser has no such argument")
    for key in sorted(set(entries) & set(parsed)):
        where, decl = f"{key[0]} {key[1]}", entries[key]
        kind = decl.get("kind")
        if kind not in KINDS:
            faults.append(f"{where}: kind {kind!r} is not one of {sorted(KINDS)}")
            continue
        if kind in JUDGED:
            site = (parsed[key], key[1] if key[1].startswith("-") else None)
            if site in rows:
                reason = rows[site]
                if reason is not None and reason.startswith(tuple(REASON_PREFIX.values())) \
                        and not reason.startswith(REASON_PREFIX[kind]):
                    faults.append(f"{where}: declared {kind}, its row answers {reason}")
            elif not (decl.get("no_row") and _test_exists(decl.get("pinned_by", ""))):
                faults.append(f"{where}: judged as {kind} but no row runs it — add one to "
                              "test_caller_paths.py, or say why not (no_row) and name the "
                              "test that pins it (pinned_by)")
        elif kind == "reader":
            if not _test_exists(decl.get("pinned_by", "")):
                faults.append(f"{where}: reader without a pinned_by test that exists")
        elif kind == "pending":
            if not isinstance(decl.get("ticket"), int):
                faults.append(f"{where}: pending without a ticket number")
            if key not in pending_ceiling:
                faults.append(f"{where}: a new pending input path — judge it, or raise "
                              "PENDING in this file with its ticket")
    pending_now = {k for k, d in entries.items() if d.get("kind") == "pending"}
    for key in sorted(pending_ceiling - pending_now):
        faults.append(f"{key[0]} {key[1]}: no longer pending — drop it from PENDING")
    return faults


# ── the gate ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def parsed():
    return parsed_arguments()


def test_every_da_tools_argument_is_declared(parsed):
    arguments, dispatched = parsed
    assert len(arguments) > 200, "the walk found too few arguments to be measuring anything"
    faults = manifest_faults(arguments, dispatched, load_manifest(), rows_by_site(), PENDING)
    assert not faults, "\n".join(faults)


# ── each rule bites (synthetic inputs, no parser walk) ───────────────────

_ROWS = {("ops/t.py", "--d"): "dir_not_accessible"}
_PARSED = {("t", "--d"): "ops/t.py", ("t", "--n"): "ops/t.py"}


def _faults(commands, *, parsed=_PARSED, rows=_ROWS, ceiling=frozenset(), dispatched=None):
    return manifest_faults(parsed, dispatched or [],
                           {"commands": commands, "dispatched_to_go": dispatched or []},
                           rows, ceiling)


def test_a_complete_manifest_has_no_fault():
    assert _faults({"t": {"--d": "dir", "--n": "not_path"}}) == []


def test_an_undeclared_argument_is_a_fault():
    assert _faults({"t": {"--d": "dir"}}) == ["t --n: takes a value and is not declared"]


def test_a_declaration_without_its_argument_is_a_fault():
    assert _faults({"t": {"--d": "dir", "--n": "not_path", "--gone": "not_path"}}) == [
        "t --gone: declared, but the parser has no such argument"]


def test_a_judged_argument_without_a_row_is_a_fault():
    faults = _faults({"t": {"--d": "dir", "--n": "dir"}})
    assert len(faults) == 1 and faults[0].startswith("t --n: judged as dir but no row")


def test_no_row_needs_a_test_that_exists():
    ok = {"kind": "dir", "no_row": "x",
          "pinned_by": "tests/shared/test_caller_path_manifest.py::test_no_row_needs_a_test_that_exists"}
    assert _faults({"t": {"--d": "dir", "--n": ok}}) == []
    bad = dict(ok, pinned_by="tests/shared/test_caller_path_manifest.py::test_not_there")
    assert len(_faults({"t": {"--d": "dir", "--n": bad}})) == 1


def test_a_kind_its_row_contradicts_is_a_fault():
    assert _faults({"t": {"--d": "file", "--n": "not_path"}}) == [
        "t --d: declared file, its row answers dir_not_accessible"]


def test_an_unknown_kind_is_a_fault():
    assert len(_faults({"t": {"--d": "dir", "--n": "path"}})) == 1


def test_a_reader_needs_its_pinning_test():
    ok = {"kind": "reader",
          "pinned_by": "tests/shared/test_caller_path_manifest.py::test_a_reader_needs_its_pinning_test"}
    assert _faults({"t": {"--d": "dir", "--n": ok}}) == []
    assert len(_faults({"t": {"--d": "dir", "--n": {"kind": "reader"}}})) == 1


def test_pending_is_a_ceiling_that_only_shrinks():
    pending = {"kind": "pending", "ticket": 1}
    key = frozenset({("t", "--n")})
    assert _faults({"t": {"--d": "dir", "--n": pending}}, ceiling=key) == []
    # a new one
    assert len(_faults({"t": {"--d": "dir", "--n": pending}})) == 1
    # one that left
    assert _faults({"t": {"--d": "dir", "--n": "not_path"}}, ceiling=key) == [
        "t --n: no longer pending — drop it from PENDING"]
    # no ticket
    assert len(_faults({"t": {"--d": "dir", "--n": {"kind": "pending"}}}, ceiling=key)) == 1


def test_the_go_dispatchers_are_named():
    assert len(_faults({"t": {"--d": "dir", "--n": "not_path"}}, dispatched=["guard"])) == 0
    assert len(manifest_faults(_PARSED, ["guard"], {"commands": {"t": {"--d": "dir",
                               "--n": "not_path"}}}, _ROWS, frozenset())) == 1
