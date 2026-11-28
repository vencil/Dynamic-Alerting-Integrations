"""#3276 G1: every value a da-tools command takes is declared, path or not.

The shared judgement of a caller-given path (`_lib_paths`) only helps where a
tool calls it, and nothing structural made a new flag call it. This gate walks
every da-tools parser (`check_cli_contract.introspect_parsers`, the walk the
CLI-contract lint already does — not a second one) and requires each argument
that takes a value to be declared in ``caller_path_manifest.yaml`` as one of:

* ``any`` — a file or a directory, judged by `_lib_paths`. It must be RUN:
  a row in ``test_caller_paths.py``'s OPS_SITES / DX_SITES, or a ``no_row``
  naming the test that pins it where a locked parent cannot reach the check.
  ⛔ There is no ``dir`` kind (#3755): a directory input is declared with
  ``_lib_input.add_input`` (below) — a stat alone read a directory it could
  not list as an empty one (#3693), and only the declaration is run on an
  unlistable, an empty and a missing directory.
  ⛔ Nor a ``file`` kind (#3797): a file input whose content the tool reads
  is a declared document (``add_input(decode=...)``, below) — judged and
  read once; a path judged here and read by the tool again was read as
  "nothing" when it was gone in between (evaluate-policy: "no policy
  rules", rc 0).
* ``reader`` — an input whose own reader judges it with a named error (a
  policy file's ``policy_unusable``, conf.d through da-guard). ``pinned_by``
  names the test.
* ``output`` — a path the tool writes (the write entry's business, #3107).
  Every one has a row in ``test_output_path_write_failure.py`` (the bad-path
  pair and the write audit); there is no exception list.
* ``runtime`` (#3983) — a path the tool creates for its own running and does
  not leave as a result: the socket a service binds, the scratch directory
  it builds in and empties. It is the third write class (#3981): a
  directory there, and every file in it, is made by the runtime entry of
  ``_lib_output`` (private to the euid: 0700 / 0600, never through a
  symlink) — only a command that declares a ``runtime`` path may call it,
  and each one that does must (``test_runtime_entry_callers.py``). No
  control run ends with a file to read, so it is not an ``output``; like a ``reader`` it is
  held by ``pinned_by``, the test that runs the tool on a path it cannot use
  — and that test's own source must spell the flag (a ``pinned_by`` naming
  some test that exists pinned nothing).
* ``url`` / ``url_list`` (#3528) — a URL the tool sends a request to (a
  ``url_list``: comma-separated URLs). Judged at parse time by
  ``_lib_validation.url_problem`` through ``type=url_type(empty=...)`` /
  ``url_list_type(...)``. Held three ways:

  1. the kind is ``url`` / ``url_list`` exactly when the parser action's
     ``type`` is a ``_lib_validation.UrlArg`` (``url_list`` when it has a
     separator) — read off the parser by identity, not by name or help;
  2. every one is RUN: ``test_a_url_flag_refuses_an_unusable_value_by_name``
     hands each a set of unusable values through the parser in-process and
     requires argparse's refusal (exit 2) naming the flag; ``""`` is refused
     for ``empty="refuse"`` and parses as ``""`` (off) for
     ``empty="disabled"``;
  3. an argument whose string default is a usable http(s) URL is a URL flag:
     it must be declared ``url`` / ``url_list`` (and so, by 1, typed).

  Scripts COMMAND_MAP does not reach (no parser to walk) are listed by hand
  in ``URL_SITES`` and held to 1 and 2 the same way.
* ``not_path`` — not a filesystem path and not a URL (names, numbers, refs).
* ``pending`` — a caller-given input path not judged yet. It carries the
  ``ticket`` that migrates it, and the set may only shrink: ``PENDING`` below
  is the ceiling, so a new pending entry is a visible edit here, not a
  quiet line in the manifest.

#3446: an argument declared with ``_lib_input.add_input`` carries its
declaration on the parser's action (``action.input_spec``) and needs no
manifest line — one there is a fault (two declarations of one flag). It
still needs a row in OPS_SITES that hands it a locked directory: that row's
argv is what ``test_caller_paths.py``'s ``test_a_declared_input_*`` rows and
``test_input_listing_audit.py`` run it with. Every one of its rows is held to
a directory: a locked directory only, a ``dir_*`` reason, one declaration
per site.

#3445: a flag declared with ``add_input(decode=Document(...))`` is a FILE
whose content the tool reads (``_lib_input.document``). It is held to
``file`` the same way: a row that hands it a locked file ({F}), a ``file_*``
reason. That row's argv is what ``test_a_declared_document_*`` (content that
cannot be read, for every declaration) and the content audit run.

A declaration the parser no longer has is a fault too: the manifest may not
outlive the flag it describes.

What a declared input is checked against: the row's own argument — a row
that hands the flag a locked DIRECTORY ({D}) proves a directory, a locked
FILE ({F}) a file; a gatherer (#3901: each value a file or a directory)
takes either. A site (script, flag) answers for one declaration only.

#3901: a value that is a file or a directory is declared with
``add_input(select=<a gatherer>)`` too (``many=True`` for several, a
positional among them), so the manifest's ``any`` kind is gone, as ``dir``
(#3755) and ``file`` (#3797) went.

⚠️ Not mechanised, so a green run does not claim it:

* Re-declaring an input as ``not_path`` / ``output`` (or a ``pending`` one as
  either, with its PENDING line dropped) is a reviewed edit of this file and
  the manifest, not something the gate can refuse: no structure tells a path
  from a name, and a vocabulary net (help text mentioning "file") would read
  as a rule while missing whatever it did not spell.
* Declaring a path the tool WRITES as ``runtime`` or ``reader`` instead of
  ``output``. An output flag that already has its row cannot leave that way
  (``test_output_path_write_failure.py``'s join the other way refuses a
  rowed flag declared ``runtime`` / ``not_path`` / ``url``); a NEW output
  flag declared ``runtime`` from the start has no row to be held by, and
  nothing structural tells a result from a scratch path.
* ⚠️ The ``reader`` road out is open: an output flag WITH a row,
  re-declared ``reader`` with any existing test for a ``pinned_by``, is
  green everywhere. A ``reader``'s ``pinned_by`` is only checked to exist
  (several share one table-driven test), and the join the other way cannot
  refuse ``reader`` — one flag with a row is rightly one
  (``state-reconcile --manifest-path``).
* A ``runtime``'s ``pinned_by`` must spell its flag in a string of the test
  (not in its docstring) — which shows the test names the flag, not that it
  runs the tool on a path it cannot use.
* The walk reads the parser the tool builds with ``DA_LANG`` unset. Three
  tools build a second, Chinese parser (alert-quality, cardinality-forecast,
  policy-engine); a flag added to that one alone is not seen.
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

KINDS = {"reader", "output", "runtime", "url", "url_list", "not_path", "pending"}
#: Kinds held by a named test rather than by a table this gate can walk.
PINNED_KINDS = {"reader", "runtime"}
URL_KINDS = {"url", "url_list"}
#: The `--json` reason prefix a declared input of each kind answers with
#: (`_lib_paths`): `any` is a gatherer's (#3901: a file or a directory).
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


def parsed_arguments(specs: "dict | None" = None, actions: "dict | None" = None,
                     ) -> tuple[dict[tuple[str, str], str], list[str]]:
    """(command, argument) -> script relative to scripts/tools, for every
    argument that takes a value; plus the commands no parser was reached for.
    *specs*, when given, is filled with (command, argument) -> the
    ``_lib_input.InputSpec`` of every argument declared with ``add_input``;
    *actions* with (command, argument) -> (the argparse action, the parser
    that owns it)."""
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
            if actions is not None:
                actions[(command, name)] = (action, parser)
            if specs is not None and getattr(action, "input_spec", None) is not None:
                specs[(command, name)] = action.input_spec

    for command, model in captured.items():
        script = cli._resolve(command_map[command]).relative_to(TOOLS).as_posix()
        walk(command, model.parser, script)
    return found, sorted(u.split()[0] for u in unscoreable)


def is_gatherer(spec: object) -> bool:
    """Whether an ``add_input`` declaration takes a file or a directory per
    value (#3901: its ``select`` gathers)."""
    return callable(getattr(getattr(spec, "select", None), "gather", None))


def is_document(spec: object) -> bool:
    """Whether an ``add_input`` declaration is a file whose content is read
    (``decode=``, #3445) rather than a directory (``select=``)."""
    return getattr(spec, "decode", None) is not None


def load_manifest(path: pathlib.Path = MANIFEST) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _entries(manifest: dict) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for command, args in (manifest.get("commands") or {}).items():
        for arg, decl in (args or {}).items():
            out[(command, arg)] = decl if isinstance(decl, dict) else {"kind": decl}
    return out


def _test_strings(ref: str) -> "list[str]":
    """The string constants of the test *ref* names (`_test_exists` holds
    for it): those of its body and of its decorators (a parametrize row),
    f-string pieces among them — NOT its docstring. A flag a test only
    talks about in prose is not a flag it runs (blind review of #4192: the
    socket test's docstring said "costs --work-dir nothing", and a
    substring search of the source took that for a pin of --work-dir)."""
    import ast
    file, _, name = ref.partition("::")
    tree = ast.parse((REPO / file).read_text(encoding="utf-8"))
    scopes = [tree.body] + [n.body for n in tree.body if isinstance(n, ast.ClassDef)]
    for body in scopes:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                doc = (node.body[0].value if node.body and isinstance(node.body[0], ast.Expr)
                       and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                       else None)
                return [c.value for part in (*node.decorator_list, *node.body)
                        for c in ast.walk(part)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)
                        and c is not doc]
    return []


def spells_flag(strings: "list[str]", flag: str) -> bool:
    """Whether one of *strings* holds *flag* as a whole option (`--work` is
    not in `--work-dir`)."""
    whole = re.compile(rf"(?<![\w-]){re.escape(flag)}(?![\w-])")
    return any(whole.search(s) for s in strings)


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


def _url_lib():
    """``_lib_validation`` — the URL judgement and its ``type=`` mark."""
    sys.path.insert(0, str(TOOLS))
    try:
        import _lib_validation
    finally:
        del sys.path[0]
    return _lib_validation


def url_faults(entries: dict[tuple[str, str], dict], actions: dict) -> list[str]:
    """#3528 rules 1 and 3: a URL flag is declared ``url``/``url_list`` exactly
    when its parser action carries the ``url_type`` mark; a usable http(s)
    URL as a string default makes it a URL flag. *actions*: (command,
    argument) -> (action, parser), or a bare action."""
    lib = _url_lib()
    faults: list[str] = []
    for key in sorted(actions):
        action = actions[key][0] if isinstance(actions[key], tuple) else actions[key]
        where = f"{key[0]} {key[1]}"
        kind = (entries.get(key) or {}).get("kind")
        mark = action.type if isinstance(action.type, lib.UrlArg) else None
        if mark is not None:
            want = "url_list" if mark.separator else "url"
            if kind != want:
                faults.append(f"{where}: parsed with {mark!r} — declare it {want}, "
                              f"not {kind}")
        elif kind in URL_KINDS:
            faults.append(f"{where}: declared {kind}, but its type is not url_type / "
                          "url_list_type — the parser does not judge it")
        default = action.default
        if isinstance(default, str) and default and lib.url_problem(default) is None \
                and kind not in URL_KINDS:
            faults.append(f"{where}: its default {default!r} is an http(s) URL — a URL "
                          "flag: declare it url, typed with url_type")
    return faults


def manifest_faults(parsed: dict[tuple[str, str], str], dispatched: list[str],
                    manifest: dict, rows: dict, pending_ceiling: frozenset,
                    specs: "dict | None" = None, actions: "dict | None" = None) -> list[str]:
    """Every way the manifest and the parsers disagree, one line each.
    *specs*: the arguments declared with ``_lib_input.add_input``;
    *actions*: (command, argument) -> (action, parser), for the URL rules."""
    faults: list[str] = []
    entries = _entries(manifest)
    specs = specs or {}
    if actions:
        faults.extend(url_faults(entries, actions))
    if sorted(manifest.get("dispatched_to_go") or []) != dispatched:
        faults.append(f"dispatched_to_go is {sorted(manifest.get('dispatched_to_go') or [])}; "
                      f"no Python parser is reached for {dispatched}")
    for key in sorted(set(parsed) - set(entries) - set(specs)):
        faults.append(f"{key[0]} {key[1]}: takes a value and is not declared")
    for key in sorted(set(specs) & set(entries)):
        faults.append(f"{key[0]} {key[1]}: declared with _lib_input.add_input — drop its "
                      "manifest line (one declaration per flag)")
    claims: dict[tuple, list[str]] = {}
    for key in sorted(set(specs) & set(parsed)):
        # An add_input selection (select=: TopLevel, DA_GUARD_LISTS) is a
        # directory, its rows held to `dir` exactly as a manifest `dir` line
        # is; a file whose content is read (decode=, #3445) is held to `file`.
        where = f"{key[0]} {key[1]}"
        site = (parsed[key], key[1] if key[1].startswith("-") else None)
        kind = ("file" if is_document(specs[key])
                else "any" if is_gatherer(specs[key]) else "dir")
        noun = {"dir": "directory", "file": "file", "any": "file or directory"}[kind]
        other = {"dir": "file", "file": "directory", "any": None}[kind]
        holders = {holder for holder, _reason in rows.get(site, [])}
        if kind == "any":
            if not holders:
                faults.append(f"{where}: declared with _lib_input.add_input but no "
                              "OPS_SITES row hands it a locked file or directory — the "
                              "declared-input rows are made from that row")
        elif kind[0].upper() not in holders:
            faults.append(f"{where}: declared with _lib_input.add_input but no "
                          f"OPS_SITES row hands it a locked {noun} — the declared-input "
                          f"rows and the {'content' if kind == 'file' else 'listing'} audit "
                          "are made from that row")
        if site in rows:
            claims.setdefault(site, []).append(where)
            for holder, reason in rows[site]:
                if other is not None and holder != kind[0].upper():
                    faults.append(f"{where}: declared with _lib_input.add_input (a "
                                  f"{noun}), its row hands it a locked {other}")
                if reason is not None and reason.startswith(tuple(REASON_PREFIX.values())) \
                        and not reason.startswith(REASON_PREFIX[kind]):
                    faults.append(f"{where}: declared with _lib_input.add_input (a "
                                  f"{noun}), its row answers {reason}")
    for key in sorted(set(entries) - set(parsed)):
        faults.append(f"{key[0]} {key[1]}: declared, but the parser has no such argument")
    # A flag declared both ways already has its fault above; its manifest
    # line is not judged a second time.
    for key in sorted((set(entries) & set(parsed)) - set(specs)):
        where, decl = f"{key[0]} {key[1]}", entries[key]
        kind = decl.get("kind")
        if kind == "dir":
            faults.append(f"{where}: a directory is declared with _lib_input.add_input, "
                          "not a manifest line (#3755: the dir kind is gone)")
            continue
        if kind == "file":
            faults.append(f"{where}: a file is declared with _lib_input.add_input(decode=...), "
                          "not a manifest line (#3797: the file kind is gone)")
            continue
        if kind == "any":
            faults.append(f"{where}: a file or a directory is declared with "
                          "_lib_input.add_input(select=<a gatherer>), not a manifest line "
                          "(#3901: the any kind is gone)")
            continue
        if kind not in KINDS:
            faults.append(f"{where}: kind {kind!r} is not one of {sorted(KINDS)}")
            continue
        if kind in PINNED_KINDS:
            if not _test_exists(decl.get("pinned_by", "")):
                faults.append(f"{where}: {kind} without a pinned_by test that exists")
            elif kind == "runtime" and not spells_flag(
                    _test_strings(decl["pinned_by"]), key[1]):
                faults.append(f"{where}: its pinned_by test never spells {key[1]} — "
                              "a test of something else pins nothing here")
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
    specs: dict = {}
    actions: dict = {}
    arguments, dispatched = parsed_arguments(specs, actions)
    return arguments, dispatched, specs, actions


def test_every_da_tools_argument_is_declared(parsed):
    arguments, dispatched, specs, actions = parsed
    assert len(arguments) > 200, "the walk found too few arguments to be measuring anything"
    assert specs, "no add_input declaration was read off a parser — the walk measured none"
    faults = manifest_faults(arguments, dispatched, load_manifest(), rows_by_site(), PENDING,
                             specs, actions)
    assert not faults, "\n".join(faults)


# ── URL flags (#3528): every one run through its parser ──────────────────

#: Values no URL flag may take: no scheme (``host:port`` parses as scheme
#: ``pushgateway``), a URL that does not parse, a port that is not a number,
#: a scheme outside the allowlist, no host; whitespace or a control character
#: (urlsplit drops \t\r\n and keeps a space in the host, so these pass it and
#: then crash the request with http.client.InvalidURL); a non-ASCII path or
#: query (sent as ASCII: UnicodeEncodeError at request time).
UNUSABLE_URLS = ("pushgateway:9091", "http://[::1", "http://h:abc", "ftp://h", "http://",
                 "http://h :80", "http://a b", "http://h\n", "http://h\t:80",
                 "http://h/m\u00e9trics", "http://h/x?q=\u00e9")

#: Scripts COMMAND_MAP does not reach: (script under scripts/tools, flag,
#: ``empty=`` mode). Their parser is built inside ``main()``; it is caught
#: at ``parse_args`` (`_parser_of`) and held to what a walked one is.
URL_SITES = [
    ("dx/run_chaos_soak.py", "--target-url", "refuse"),
    ("dx/inject_waveform.py", "--vm-url", "refuse"),
    ("ops/_federation_revocation_reconciler.py", "--victorialogs-url", "refuse"),
]


class _Caught(BaseException):
    """Raised from a patched ``parse_args`` to carry the parser out of main()
    — a BaseException, so no ``except Exception`` in the tool swallows it."""


def _parser_of(script: str, monkeypatch):
    """The parser *script*'s ``main()`` builds, caught before it parses."""
    import argparse
    path = TOOLS / script
    spec = importlib.util.spec_from_file_location(f"_url_site_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # a @dataclass looks itself up
    spec.loader.exec_module(module)

    def catch(self, *_a, **_k):
        raise _Caught(self)
    monkeypatch.setattr(argparse.ArgumentParser, "parse_args", catch)
    try:
        module.main()
    except _Caught as caught:
        return caught.args[0]
    raise AssertionError(f"{script}: main() never reached parse_args")


def _action_named(parser, flag: str):
    return next(a for a in parser._actions if flag in a.option_strings)


def _refusal(parser, argv: list[str]) -> "tuple[object, str]":
    """(the SystemExit code, or the parsed namespace when it parsed; stderr)."""
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            namespace = parser.parse_args(argv)
        except SystemExit as exc:
            return exc.code, err.getvalue()
    return namespace, err.getvalue()


def url_value_faults(where: str, action, parser) -> list[str]:
    """Hand an argument each unusable value through *parser* (in-process):
    argparse must refuse it (exit 2) with a line naming the argument. ``""``
    is refused for ``empty="refuse"`` and parses as off for ``"disabled"``."""
    import argparse
    lib = _url_lib()
    mark = action.type
    if not isinstance(mark, lib.UrlArg):
        return [f"{where}: not typed with url_type"]
    flag = action.option_strings[0] if action.option_strings else None
    named = "argument " + ("/".join(action.option_strings) if flag else action.metavar
                           or action.dest) + ":"
    bad = list(UNUSABLE_URLS)
    if mark.separator:  # one unusable item among usable ones is refused too
        bad += [f"http://127.0.0.1:9090{mark.separator}{v}" for v in UNUSABLE_URLS]
    if mark.empty == "refuse":
        bad.append("")
    faults = []
    for value in bad:
        got, err = _refusal(parser, [flag, value] if flag else [value])
        if got != 2 or named not in err:
            faults.append(f"{where} {value!r}: wanted exit 2 naming {named!r}, got "
                          f"{got!r}: {err.strip()[-300:]}")
    if mark.empty == "disabled":
        # Only this argument is asked about: the parser's other required
        # arguments are let off for the one parse, then put back.
        held = [a for a in parser._actions if a.required and a is not action]
        groups = [g for g in parser._mutually_exclusive_groups if g.required]
        for a in held:
            a.required = False
        for g in groups:
            g.required = False
        try:
            got, err = _refusal(parser, [flag, ""])
        finally:
            for a in held:
                a.required = True
            for g in groups:
                g.required = True
        off = [] if mark.separator else ""
        if not isinstance(got, argparse.Namespace) or getattr(got, action.dest) != off:
            faults.append(f"{where} '': empty=\"disabled\" must parse as {off!r}, got "
                          f"{got!r}: {err.strip()[-300:]}")
    return faults


def test_a_url_flag_refuses_an_unusable_value_by_name(parsed):
    _arguments, _dispatched, _specs, actions = parsed
    declared = {k for k, d in _entries(load_manifest()).items() if d.get("kind") in URL_KINDS}
    assert len(declared) > 20, "too few url declarations to be measuring anything"
    faults: list[str] = []
    for key in sorted(declared & set(actions)):
        action, parser = actions[key]
        faults += url_value_faults(f"{key[0]} {key[1]}", action, parser)
    assert not faults, "\n".join(faults)


@pytest.mark.parametrize("script, flag, empty", URL_SITES, ids=[f"{s} {f}" for s, f, _e in URL_SITES])
def test_a_url_flag_outside_command_map_is_held_the_same_way(monkeypatch, script, flag, empty):
    parser = _parser_of(script, monkeypatch)
    monkeypatch.undo()  # parse_args back, for the rows below
    action = _action_named(parser, flag)
    lib = _url_lib()
    assert isinstance(action.type, lib.UrlArg) and not action.type.separator, \
        f"{script} {flag}: not typed with url_type ({action.type!r})"
    assert action.type.empty == empty, (script, flag, action.type)
    faults = url_faults({(script, flag): {"kind": "url"}}, {(script, flag): action})
    faults += url_value_faults(f"{script} {flag}", action, parser)
    assert not faults, "\n".join(faults)


def test_no_other_argument_of_a_url_site_has_a_url_default(monkeypatch):
    """Rule 3 for the hand-listed scripts: a usable http(s) URL default on
    an argument URL_SITES does not list is a URL flag no row holds."""
    listed = {(s, f) for s, f, _e in URL_SITES}
    faults: list[str] = []
    for script in sorted({s for s, _f, _e in URL_SITES}):
        parser = _parser_of(script, monkeypatch)
        monkeypatch.undo()
        for action in parser._actions:
            if not action.option_strings or action.nargs == 0:
                continue
            key = (script, action.option_strings[-1] if action.option_strings[-1].startswith("--")
                   else action.option_strings[0])
            entries = {key: {"kind": "url"}} if key in listed else {}
            faults += url_faults(entries, {key: action})
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

_ROWS = {("ops/t.py", "--d"): [("F", "path_not_accessible")]}
_PARSED = {("t", "--d"): "ops/t.py", ("t", "--n"): "ops/t.py"}


def _faults(commands, *, parsed=_PARSED, rows=_ROWS, ceiling=frozenset(), dispatched=None):
    return manifest_faults(parsed, dispatched or [],
                           {"commands": commands, "dispatched_to_go": dispatched or []},
                           rows, ceiling)


def test_a_complete_manifest_has_no_fault():
    assert _faults({"t": {"--d": "not_path", "--n": "not_path"}}) == []


def test_an_undeclared_argument_is_a_fault():
    assert _faults({"t": {"--d": "not_path"}}) == ["t --n: takes a value and is not declared"]


def test_a_declaration_without_its_argument_is_a_fault():
    assert _faults({"t": {"--d": "not_path", "--n": "not_path", "--gone": "not_path"}}) == [
        "t --gone: declared, but the parser has no such argument"]


def test_a_manifest_any_line_is_a_fault():
    """#3901: a file-or-directory input is declared with add_input (a
    gatherer), never a manifest ``any`` line — and none is left."""
    assert _faults({"t": {"--d": "any", "--n": "not_path"}}) == [
        "t --d: a file or a directory is declared with _lib_input.add_input(select=<a "
        "gatherer>), not a manifest line (#3901: the any kind is gone)"]
    assert not [k for k, d in _entries(load_manifest()).items() if d.get("kind") == "any"]


class _Gatherer:
    class select:  # noqa: N801 — a stand-in for the selector on a spec
        @staticmethod
        def gather(targets, exclude=()):
            return []


def test_a_gatherer_takes_a_row_of_either_holder_and_answers_path_reasons():
    """#3901: what the `any` line held, held on the declaration: a row of
    either holder, a `path_*` reason, and a row at all."""
    parsed = {("t", "--d"): "ops/t.py"}
    specs = {("t", "--d"): _Gatherer()}
    for holder in ("D", "F"):
        rows = {("ops/t.py", "--d"): [(holder, "path_not_accessible")]}
        assert manifest_faults(parsed, [], {"commands": {}}, rows, frozenset(), specs) == []
    wrong = {("ops/t.py", "--d"): [("D", "dir_not_accessible")]}
    assert manifest_faults(parsed, [], {"commands": {}}, wrong, frozenset(), specs) == [
        "t --d: declared with _lib_input.add_input (a file or directory), its row answers "
        "dir_not_accessible"]
    faults = manifest_faults(parsed, [], {"commands": {}}, {}, frozenset(), specs)
    assert len(faults) == 1 and "no OPS_SITES row hands it a locked file or directory" in faults[0]
    # a positional's site is (script, None)
    pos = {("ops/t.py", None): [("D", None), ("F", None)]}
    assert manifest_faults({("t", "paths"): "ops/t.py"}, [], {"commands": {}}, pos, frozenset(),
                           {("t", "paths"): _Gatherer()}) == []


def test_a_pinning_test_is_a_collected_test_function(tmp_path):
    here = "tests/shared/test_caller_path_manifest.py"
    assert _test_exists(f"{here}::test_a_pinning_test_is_a_collected_test_function")
    # a method of a test class counts
    assert _test_exists("tests/lint/test_check_cli_contract.py::"
                        "test_the_tree_is_green_under_the_ledger")
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
    assert len(_faults({"t": {"--d": "not_path", "--n": "path"}})) == 1


def test_a_reader_needs_its_pinning_test():
    ok = {"kind": "reader",
          "pinned_by": "tests/shared/test_caller_path_manifest.py::test_a_reader_needs_its_pinning_test"}
    assert _faults({"t": {"--d": "not_path", "--n": ok}}) == []
    assert len(_faults({"t": {"--d": "not_path", "--n": {"kind": "reader"}}})) == 1


def test_a_runtime_path_needs_its_pinning_test():
    """#3983: a socket or a scratch directory is not an ``output`` with an
    exception — it is its own kind, held the way a ``reader`` is."""
    here = "tests/shared/test_caller_path_manifest.py"
    ok = {"kind": "runtime", "pinned_by": f"{here}::test_a_runtime_path_needs_its_pinning_test"}
    assert _faults({"t": {"--d": "not_path", "--n": ok}}) == []
    # a test that exists and is about something else pins nothing: it must
    # spell the flag (this one spells --n and --d; the third is put together
    # here so that this source does not spell it)
    absent = "--" + "z" * 2
    assert _faults({"t": {"--d": "not_path", "--n": "not_path", absent: ok}},
                   parsed={**_PARSED, ("t", absent): "ops/t.py"}) == [
        f"t {absent}: its pinned_by test never spells {absent} — a test of something "
        "else pins nothing here"]
    assert _faults({"t": {"--d": "not_path", "--n": {"kind": "runtime"}}}) == [
        "t --n: runtime without a pinned_by test that exists"]
    # …in a STRING of the test: the docstring above names `runtime` and
    # `output` and `reader`; none of them is a string this test holds
    strings = _test_strings(f"{here}::test_a_runtime_path_needs_its_pinning_test")
    assert "--n" in strings
    # (the first line: Python 3.13 dedents `__doc__`, the syntax tree does not)
    first = test_a_runtime_path_needs_its_pinning_test.__doc__.splitlines()[0]
    assert first and not any(s.startswith(first) for s in strings)
    # …so a test that only TALKS about a flag does not pin it. The name is
    # put together here: no string of this file's tests spells it.
    prose_only = "--zq" + "-only-in-prose"
    talks = {"kind": "runtime",
             "pinned_by": f"{here}::test_a_test_that_only_talks_about_a_flag"}
    assert _faults({"t": {"--d": "not_path", "--n": "not_path", prose_only: talks}},
                   parsed={**_PARSED, ("t", prose_only): "ops/t.py"}) == [
        f"t {prose_only}: its pinned_by test never spells {prose_only} — a test of "
        "something else pins nothing here"]
    # a whole option, not the start of a longer one
    assert spells_flag(["ERROR: --work-dir /x: no"], "--work-dir")
    assert not spells_flag(["--work-dir"], "--work")
    assert not spells_flag(["x--work-dirs"], "--work-dir")
    # a parametrize row is a string of the test too
    assert "decorated" in _test_strings(f"{here}::test_a_decorated_test_shows_its_rows")

    gone = {"kind": "runtime", "pinned_by": f"{here}::test_no_such_test"}
    assert len(_faults({"t": {"--d": "not_path", "--n": gone}})) == 1
    declared = {k: d for k, d in _entries(load_manifest()).items()
                if d.get("kind") == "runtime"}
    assert declared, "no runtime path is declared — the rule above holds nothing"
    assert all(_test_exists(d.get("pinned_by", "")) for d in declared.values()), declared


def test_a_test_that_only_talks_about_a_flag():
    """A fixture of the test above: it says --zq-only-in-prose here, in its
    docstring, and holds no string that does."""


@pytest.mark.parametrize("row", ["decorated"])
def test_a_decorated_test_shows_its_rows(row):
    """The fixture of the last assertion above: its only string is in its
    decorator."""
    assert row


def test_a_manifest_file_line_is_a_fault():
    """#3797: a file input is a declared document, never a manifest ``file``
    line — and none is left."""
    assert _faults({"t": {"--d": "file", "--n": "not_path"}}) == [
        "t --d: a file is declared with _lib_input.add_input(decode=...), not a manifest "
        "line (#3797: the file kind is gone)"]
    assert not [k for k, d in _entries(load_manifest()).items() if d.get("kind") == "file"]


def test_a_manifest_dir_line_is_a_fault():
    """#3755: a directory input is declared with add_input, never a manifest
    ``dir`` line — and none is left."""
    assert _faults({"t": {"--d": "dir", "--n": "not_path"}}) == [
        "t --d: a directory is declared with _lib_input.add_input, not a manifest line "
        "(#3755: the dir kind is gone)"]
    assert not [k for k, d in _entries(load_manifest()).items() if d.get("kind") == "dir"]


def test_pending_is_a_ceiling_that_only_shrinks():
    pending = {"kind": "pending", "ticket": 1}
    key = frozenset({("t", "--n")})
    assert _faults({"t": {"--d": "not_path", "--n": pending}}, ceiling=key) == []
    # a new one
    assert len(_faults({"t": {"--d": "not_path", "--n": pending}})) == 1
    # one that left
    assert _faults({"t": {"--d": "not_path", "--n": "not_path"}}, ceiling=key) == [
        "t --n: no longer pending — drop it from PENDING"]
    # no ticket
    assert len(_faults({"t": {"--d": "not_path", "--n": {"kind": "pending"}}}, ceiling=key)) == 1


def test_the_go_dispatchers_are_named():
    assert len(_faults({"t": {"--d": "not_path", "--n": "not_path"}}, dispatched=["guard"])) == 0
    assert len(manifest_faults(_PARSED, ["guard"], {"commands": {"t": {"--d": "not_path",
                               "--n": "not_path"}}}, _ROWS, frozenset())) == 1


def test_an_add_input_flag_needs_no_manifest_line_and_may_not_have_one():
    """#3446: the declaration on the parser replaces the manifest line."""
    spec = object()
    parsed = {("t", "--d"): "ops/t.py", ("t", "--n"): "ops/t.py"}
    assert _faults({"t": {"--n": "not_path"}}, parsed=parsed) == [
        "t --d: takes a value and is not declared"]
    dirs = {("ops/t.py", "--d"): [("D", "dir_not_accessible")]}
    assert manifest_faults(parsed, [], {"commands": {"t": {"--n": "not_path"}}},
                           dirs, frozenset(), {("t", "--d"): spec}) == []
    assert manifest_faults(parsed, [], {"commands": {"t": {"--d": "not_path", "--n": "not_path"}}},
                           dirs, frozenset(), {("t", "--d"): spec}) == [
        "t --d: declared with _lib_input.add_input — drop its manifest line "
        "(one declaration per flag)"]


def test_an_add_input_flag_needs_a_locked_directory_row():
    spec = object()
    parsed = {("t", "--d"): "ops/t.py"}
    faults = manifest_faults(parsed, [], {"commands": {}}, {}, frozenset(),
                             {("t", "--d"): spec})
    assert len(faults) == 1 and "no OPS_SITES row hands it a locked directory" in faults[0]
    rows = {("ops/t.py", "--d"): [("F", None)]}
    faults = manifest_faults(parsed, [], {"commands": {}}, rows, frozenset(),
                             {("t", "--d"): spec})
    assert len(faults) == 2 and "its row hands it a locked file" in faults[1], faults


def test_an_add_input_flag_is_held_to_dir_like_a_manifest_dir_line():
    """#3446 review: a declaration on the parser may not loosen what a
    manifest `dir` line demands of its rows."""
    spec = object()
    parsed = {("t", "--d"): "ops/t.py"}
    ok = {("ops/t.py", "--d"): [("D", "dir_not_found")]}
    assert manifest_faults(parsed, [], {"commands": {}}, ok, frozenset(),
                           {("t", "--d"): spec}) == []
    # a D row besides an F row: the F row is still a fault
    mixed = {("ops/t.py", "--d"): [("D", None), ("F", None)]}
    assert manifest_faults(parsed, [], {"commands": {}}, mixed, frozenset(),
                           {("t", "--d"): spec}) == [
        "t --d: declared with _lib_input.add_input (a directory), its row hands it a locked file"]
    # a row that answers with a file_* reason
    wrong = {("ops/t.py", "--d"): [("D", "file_not_found")]}
    assert manifest_faults(parsed, [], {"commands": {}}, wrong, frozenset(),
                           {("t", "--d"): spec}) == [
        "t --d: declared with _lib_input.add_input (a directory), its row answers file_not_found"]
    # one site answering for two declarations (two commands of one script)
    two = {("t", "--d"): "ops/t.py", ("u", "--d"): "ops/t.py"}
    either = {("ops/t.py", "--d"): [("D", None)]}
    faults = manifest_faults(two, [], {"commands": {}}, either, frozenset(),
                             {("t", "--d"): spec, ("u", "--d"): spec})
    assert faults == ["t --d / u --d: one row (ops/t.py --d) cannot answer for more than "
                      "one declaration"], faults


def test_an_add_input_document_is_held_to_file():
    """#3445: a `decode=` declaration (a file whose content is read) is held
    to `file` as a manifest `file` line is: a locked-file row, `file_*`."""
    class _Doc:
        decode = object()
    spec = _Doc()
    parsed = {("t", "--f"): "ops/t.py"}
    ok = {("ops/t.py", "--f"): [("F", "file_not_accessible")]}
    assert manifest_faults(parsed, [], {"commands": {}}, ok, frozenset(),
                           {("t", "--f"): spec}) == []
    faults = manifest_faults(parsed, [], {"commands": {}}, {}, frozenset(),
                             {("t", "--f"): spec})
    assert len(faults) == 1 and "no OPS_SITES row hands it a locked file" in faults[0], faults
    dir_row = {("ops/t.py", "--f"): [("F", None), ("D", None)]}
    assert manifest_faults(parsed, [], {"commands": {}}, dir_row, frozenset(),
                           {("t", "--f"): spec}) == [
        "t --f: declared with _lib_input.add_input (a file), its row hands it a locked directory"]
    wrong = {("ops/t.py", "--f"): [("F", "dir_not_found")]}
    assert manifest_faults(parsed, [], {"commands": {}}, wrong, frozenset(),
                           {("t", "--f"): spec}) == [
        "t --f: declared with _lib_input.add_input (a file), its row answers dir_not_found"]


# ── the URL rules bite (synthetic parsers, no walk) ─────────────────────

def _url_parser():
    import argparse
    lib = _url_lib()
    p = argparse.ArgumentParser(prog="t")
    p.add_argument("--u", type=lib.url_type(empty="refuse"))
    p.add_argument("--off", type=lib.url_type(empty="disabled"))
    p.add_argument("--list", type=lib.url_list_type(empty="refuse"))
    p.add_argument("--n")
    p.add_argument("--need", required=True)
    return p


def _actions_of(p) -> dict:
    return {("t", a.option_strings[0]): (a, p) for a in p._actions if a.option_strings
            and a.option_strings[0] != "-h"}


def test_a_url_declaration_is_read_off_the_parser_by_identity():
    p = _url_parser()
    ok = {"--u": "url", "--off": "url", "--list": "url_list", "--n": "not_path",
          "--need": "not_path"}
    assert url_faults(_entries({"commands": {"t": ok}}), _actions_of(p)) == []
    # a url_type flag declared not_path; a list declared url; a plain flag declared url
    wrong = dict(ok, **{"--u": "not_path", "--list": "url", "--n": "url"})
    faults = url_faults(_entries({"commands": {"t": wrong}}), _actions_of(p))
    assert faults == [
        "t --list: parsed with UrlArg(empty='refuse', separator=',') — declare it url_list, "
        "not url",
        "t --n: declared url, but its type is not url_type / url_list_type — the parser "
        "does not judge it",
        "t --u: parsed with UrlArg(empty='refuse') — declare it url, not not_path"], faults


def test_a_name_that_looks_like_a_url_flag_is_not_the_mark():
    """Identity, not vocabulary: a plain callable named like the lib's is not
    a url_type."""
    import argparse
    p = argparse.ArgumentParser(prog="t")

    def url(value):
        return value
    p.add_argument("--u", type=url)
    faults = url_faults({("t", "--u"): {"kind": "url"}}, _actions_of(p))
    assert len(faults) == 1 and "its type is not url_type" in faults[0], faults


def test_a_url_default_makes_a_url_flag():
    import argparse
    p = argparse.ArgumentParser(prog="t")
    p.add_argument("--n", default="http://localhost:9093")
    p.add_argument("--m", default="localhost:9093")  # not a usable URL: not this rule
    faults = url_faults({("t", "--n"): {"kind": "not_path"}, ("t", "--m"): {"kind": "not_path"}},
                        _actions_of(p))
    assert faults == ["t --n: its default 'http://localhost:9093' is an http(s) URL — a URL "
                      "flag: declare it url, typed with url_type"], faults


def test_the_value_rows_refuse_by_name_and_let_disabled_be_empty():
    p = _url_parser()
    acts = _actions_of(p)
    for flag in ("--u", "--off", "--list"):
        action, parser = acts[("t", flag)]
        assert url_value_faults(f"t {flag}", action, parser) == [], flag
    # the rows notice a type that lets an unusable value through
    lib = _url_lib()
    action, parser = acts[("t", "--u")]
    loose = lib.UrlArg("refuse")
    loose._one = lambda value: value  # accepts anything
    action.type = loose
    faults = url_value_faults("t --u", action, parser)
    assert len(faults) == len(UNUSABLE_URLS), faults
    # and the required arguments the "" row let off are put back
    assert acts[("t", "--need")][0].required


def test_url_problem_is_the_one_judgement():
    lib = _url_lib()
    for good in ("http://localhost:9090", "https://prom.example:443/x", "http://[::1]:9090",
                 # IDNA host; a percent-encoded path; a fragment is never sent
                 "http://\u4f8b\u3048.jp/metrics", "http://h/a%C3%A9", "http://h/#\u00e9"):
        assert lib.url_problem(good) is None, good
    for bad in UNUSABLE_URLS + ("", "http://h:99999", "file:///etc/passwd"):
        assert lib.url_problem(bad), bad
    sys.path.insert(0, str(TOOLS))
    try:
        import _lib_prometheus
    finally:
        del sys.path[0]
    # _validate_url_scheme keeps its Optional[str] contract over the same judgement
    for value in ("http://localhost:9090",) + UNUSABLE_URLS:
        assert _lib_prometheus._validate_url_scheme(value) == lib.url_problem(value), value


def test_a_url_list_refuses_an_empty_separator():
    """``separator=""`` would parse as one URL, not the list it promises."""
    lib = _url_lib()
    with pytest.raises(ValueError, match="separator"):
        lib.url_list_type(empty="refuse", separator="")
