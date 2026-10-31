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

What a judged kind is checked against: the row's own argument — a row that
hands the flag a locked DIRECTORY ({D}) proves ``dir``, a locked FILE ({F})
``file``; ``any`` takes either. A site (script, flag) answers for one
declaration only.

⚠️ Not mechanised, so a green run does not claim it:

* Re-declaring an input as ``not_path`` / ``output`` (or a ``pending`` one as
  either, with its PENDING line dropped) is a reviewed edit of this file and
  the manifest, not something the gate can refuse: no structure tells a path
  from a name, and a vocabulary net (help text mentioning "file") would read
  as a rule while missing whatever it did not spell.
* The walk reads the parser the tool builds with ``DA_LANG`` unset. Four
  tools build a second, Chinese parser (alert-quality, cardinality-forecast,
  policy-engine, opa-evaluate); a flag added to that one alone is not seen.
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
#: #3392's (missing read as empty) and #3415's (traceback, misworded,
#: silent) are all judged now: none is pending.
PENDING: frozenset[tuple[str, str]] = frozenset()


def _caller_paths():
    """test_caller_paths.py, for its OPS_SITES / DX_SITES tables."""
    spec = importlib.util.spec_from_file_location(
        "_caller_paths_tables", pathlib.Path(__file__).with_name("test_caller_paths.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _locked_argument(argv: list[str]) -> tuple[str | None, str]:
    """(the flag, or None for a positional; "D" or "F") of the argument a row
    hands the locked parent — read from the row's argv, not its label."""
    # {NX}: a directory that stats but cannot be entered — a directory too.
    for i, token in enumerate(argv):
        for holder, means in (("D", "D"), ("NX", "D"), ("F", "F")):
            if "{" + holder + "}" in token:
                before = argv[i - 1] if i else ""
                return (before if before.startswith("-") else None), means
    raise AssertionError(f"a row with no locked argument: {argv}")


def rows_by_site() -> dict[tuple[str, str | None], list[tuple[str, str | None]]]:
    """(script relative to scripts/tools, flag or None) -> one (D|F, the row's
    `--json` reason or None) per row that runs it."""
    tables = _caller_paths()
    rows: dict[tuple[str, str | None], list[tuple[str, str | None]]] = {}
    for _id, script, argv, _flag, reason in tables.OPS_SITES:
        flag, holder = _locked_argument(argv)
        rows.setdefault((f"ops/{script}", flag), []).append((holder, reason))
    for _id, script, argv, _named, _rc in tables.DX_SITES:
        flag, holder = _locked_argument(argv)
        rows.setdefault((script, flag), []).append((holder, None))
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
    """`tests/…/test_x.py::test_name` names a test pytest collects: a
    ``test_*`` function at module level or in a class, in a ``test_*.py``
    under tests/ — read from the syntax tree, so a docstring that spells
    ``def test_x(`` is not one."""
    import ast
    file, _, name = ref.partition("::")
    path = REPO / file
    if not (file.startswith("tests/") and path.name.startswith("test_")
            and path.suffix == ".py" and name.startswith("test_") and path.is_file()):
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scopes = [tree.body] + [n.body for n in tree.body if isinstance(n, ast.ClassDef)]
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
               for body in scopes for n in body)


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
    claims: dict[tuple, list[str]] = {}
    for key in sorted(set(entries) & set(parsed)):
        where, decl = f"{key[0]} {key[1]}", entries[key]
        kind = decl.get("kind")
        if kind not in KINDS:
            faults.append(f"{where}: kind {kind!r} is not one of {sorted(KINDS)}")
            continue
        if kind in JUDGED:
            site = (parsed[key], key[1] if key[1].startswith("-") else None)
            if site in rows:
                claims.setdefault(site, []).append(where)
                for holder, reason in rows[site]:
                    if kind != "any" and holder != kind[0].upper():
                        faults.append(f"{where}: declared {kind}, its row hands it a locked "
                                      f"{'directory' if holder == 'D' else 'file'}")
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
    # A site's flag is None for a positional: sort it before any option
    # string of the same script (a script may have both, #3426).
    for site, claimants in sorted(claims.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        if len(claimants) > 1:
            faults.append(f"{' / '.join(claimants)}: one row ({site[0]} {site[1]}) "
                          "cannot answer for more than one declaration")
    pending_now = {k for k, d in entries.items() if d.get("kind") == "pending"}
    for key in sorted(pending_ceiling - pending_now):
        faults.append(f"{key[0]} {key[1]}: no longer pending — drop it from PENDING")
    return faults


# ── readers no other test runs on a missing input ────────────────────────

_MISSING = "/nonexistent/g1-missing"
_CONF = str(REPO / "tests" / "golden" / "fixtures" / "flat" / "conf.d")
_AM = str(REPO / "try-local" / "alertmanager.yml")

#: (command, argument, script under scripts/tools, argv, the reader's own
#: words). {M} is a path with nothing there, {OUT} a scratch directory. Each
#: row: rc 2, the reader's refusal naming the path — not a traceback, not a
#: catch-all that turned some other exception into rc 2 (that also names the
#: path, so the words are what tells the two apart).
_GUARD_STAT = 'stat "' + _MISSING + '"'
READER_SITES = [
    ("backtest", "--config-dir", "ops/backtest_threshold.py",
     ["--config-dir", "{M}", "--baseline", _CONF, "--json"], "da-guard served-values: " + _GUARD_STAT),
    ("backtest", "--baseline", "ops/backtest_threshold.py",
     ["--config-dir", _CONF, "--baseline", "{M}"], "da-guard served-values: " + _GUARD_STAT),
    ("deprecate", "--root-dir", "ops/deprecate_rule.py",
     ["--config-dir", _CONF, "--plane", "subtree", "--root-dir", "{M}", "some_metric"],
     "da-guard parse-tree: " + _GUARD_STAT),
    ("offboard", "--config-dir", "ops/offboard_tenant.py", ["tenant-a", "--config-dir", "{M}"],
     "da-guard parse-tree: " + _GUARD_STAT),
    ("opa-evaluate", "--config-dir", "ops/policy_opa_bridge.py",
     ["--config-dir", "{M}", "--opa-url", "http://127.0.0.1:9"],
     "da-guard served-values: " + _GUARD_STAT),
    ("explain-keys", "--da-guard-binary", "ops/explain_keys.py",
     ["--config-dir", _CONF, "--da-guard-binary", "{M}"],
     f"da-guard binary not found at '{_MISSING}'"),
    ("onboard", "--da-crdecode-binary", "ops/onboard_platform.py",
     ["--alertmanager-config", _AM, "--da-crdecode-binary", "{M}", "--output-dir", "{OUT}"],
     f"cannot run da-crdecode ({_MISSING})"),
    ("onboard", "--da-read-binary", "ops/onboard_platform.py",
     ["--alertmanager-config", _AM, "--da-read-binary", "{M}", "--output-dir", "{OUT}"],
     f"cannot run da-read ({_MISSING})"),
]
_READER_TEST = ("tests/shared/test_caller_path_manifest.py::"
                "test_a_reader_refuses_a_missing_input_by_name")
#: What a catch-all says when it turned an unexpected exception into rc 2.
_CRASH_WORDS = ("Traceback", "could not finish", "unexpected internal error")


@pytest.mark.usefixtures("da_guard_env", "da_read_env")
@pytest.mark.parametrize("command, argument, script, argv, words", READER_SITES,
                         ids=[f"{c} {a}" for c, a, _s, _v, _w in READER_SITES])
def test_a_reader_refuses_a_missing_input_by_name(tmp_path, command, argument, script, argv,
                                                   words):
    import subprocess
    args = [a.replace("{M}", _MISSING).replace("{OUT}", str(tmp_path / "out")) for a in argv]
    proc = subprocess.run([sys.executable, str(TOOLS / script), *args], capture_output=True,
                          text=True, encoding="utf-8", timeout=300, stdin=subprocess.DEVNULL)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert not [w for w in _CRASH_WORDS if w in out], out[-3000:]
    assert words in out, out[-3000:]


_KUBECTL = ("#!/bin/sh\n"
            "# Stands in for kubectl refusing a kubeconfig that is not there.\n"
            "while [ $# -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    --kubeconfig) echo \"error: stat $2: no such file or directory\" >&2; exit 1;;\n"
            "    --kubeconfig=*) echo \"error: stat ${1#--kubeconfig=}: no such file or directory\" >&2;"
            " exit 1;;\n"
            "  esac\n  shift\ndone\nexit 0\n")
_KUBECTL_TEST = ("tests/shared/test_caller_path_manifest.py::"
                 "test_kubectls_refusal_of_a_kubeconfig_is_relayed")
KUBECONFIG_SITES = [
    ("drift-detect", "ops/drift_detect.py", ["--mode", "operator", "--dirs", _CONF]),
    ("operator-check", "ops/operator_check.py",
     ["--rule-packs-dir", str(REPO / "rule-packs")]),
]


@pytest.mark.parametrize("command, script, argv", KUBECONFIG_SITES,
                         ids=[c for c, _s, _v in KUBECONFIG_SITES])
def test_kubectls_refusal_of_a_kubeconfig_is_relayed(tmp_path, command, script, argv):
    """--kubeconfig is judged by kubectl; what the tool owes is to relay that
    refusal as a caller error, not to read it as an empty cluster. A stand-in
    kubectl answers the way kubectl does for a kubeconfig that is not there
    (CI has no kubectl to ask)."""
    import os
    import subprocess
    fake = tmp_path / "bin" / "kubectl"
    fake.parent.mkdir()
    fake.write_text(_KUBECTL, encoding="utf-8")
    fake.chmod(0o755)
    proc = subprocess.run([sys.executable, str(TOOLS / script), *argv, "--kubeconfig", _MISSING],
                          capture_output=True, text=True, encoding="utf-8", timeout=120,
                          env={**os.environ,
                               "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out[-3000:]
    assert not [w for w in _CRASH_WORDS if w in out], out[-3000:]
    assert f"stat {_MISSING}: no such file or directory" in out, out[-3000:]


def test_the_go_dispatchers_dispatch_to_go():
    """`dispatched_to_go` takes a command out of the walk; only a script that
    is a Go dispatcher (`_lib_godispatch`) may be there."""
    sys.path[:0] = [str(TOOLS / "lint"), str(TOOLS)]
    try:
        import check_cli_contract as cli
    finally:
        del sys.path[:2]
    command_map = cli.parse_command_map()
    for command in load_manifest()["dispatched_to_go"]:
        script = cli._resolve(command_map[command])
        assert script.name.endswith("_dispatch.py") and "_lib_godispatch" in \
            script.read_text(encoding="utf-8"), f"{command}: {script} is not a Go dispatcher"


# ── the gate ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def parsed():
    return parsed_arguments()


def test_every_da_tools_argument_is_declared(parsed):
    arguments, dispatched = parsed
    assert len(arguments) > 200, "the walk found too few arguments to be measuring anything"
    faults = manifest_faults(arguments, dispatched, load_manifest(), rows_by_site(), PENDING)
    assert not faults, "\n".join(faults)


def test_a_reader_pinned_here_has_its_row():
    """A `pinned_by` naming the reader table is only true for a row it has."""
    for test, rows in ((_READER_TEST, {(c, a) for c, a, _s, _v, _w in READER_SITES}),
                       (_KUBECTL_TEST, {(c, "--kubeconfig") for c, _s, _v in KUBECONFIG_SITES})):
        claimed = {k for k, d in _entries(load_manifest()).items()
                   if d.get("kind") == "reader" and d.get("pinned_by") == test}
        assert claimed == rows, (test, {"claimed, no row": claimed - rows,
                                        "row, not claimed": rows - claimed})


# ── each rule bites (synthetic inputs, no parser walk) ───────────────────

_ROWS = {("ops/t.py", "--d"): [("D", "dir_not_accessible")]}
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
        "t --d: declared file, its row hands it a locked directory",
        "t --d: declared file, its row answers dir_not_accessible"]


def test_a_kind_is_read_off_the_row_even_without_a_reason():
    rows = {("ops/t.py", "--d"): [("D", None)]}
    assert _faults({"t": {"--d": "dir", "--n": "not_path"}}, rows=rows) == []
    assert _faults({"t": {"--d": "any", "--n": "not_path"}}, rows=rows) == []
    assert _faults({"t": {"--d": "file", "--n": "not_path"}}, rows=rows) == [
        "t --d: declared file, its row hands it a locked directory"]


def test_one_row_answers_for_one_declaration():
    parsed = {("t", "--d"): "ops/t.py", ("u", "--d"): "ops/t.py"}
    faults = _faults({"t": {"--d": "dir"}, "u": {"--d": "dir"}}, parsed=parsed)
    assert faults == ["t --d / u --d: one row (ops/t.py --d) cannot answer for more than "
                      "one declaration"]


def test_a_pinning_test_is_a_collected_test_function(tmp_path):
    here = "tests/shared/test_caller_path_manifest.py"
    assert _test_exists(f"{here}::test_a_pinning_test_is_a_collected_test_function")
    # a method of a test class counts
    assert _test_exists("tests/ops/test_migrate_rule_outputs.py::"
                        "test_missing_input_file_exits_caller_error")
    # product code, a conftest docstring, a non-test name: not tests
    assert not _test_exists("scripts/tools/ops/lint_custom_rules.py::load_policy")
    assert not _test_exists("tests/conftest.py::test_x")
    assert not _test_exists(f"{here}::_faults")
    # spelled only inside a string here
    assert not _test_exists(f"{here}::test_spelled_only_in_a_string")


_SPELLED = """
def test_spelled_only_in_a_string():
    pass
"""


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
