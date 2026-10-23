"""_lib_confedit.py — a Python tool's change to a conf.d file, made by Go (ADR-036 §2).

ADR-036 §2: only Go writes conf.d. A Python tool that changes an EXISTING
conf.d file (ADR-036 step 10b, #3206) does not produce the file's text
itself: it says what to change — `set` / `unset` / `ensure-map` operations
on key paths, each value with its type — and `da-guard edit` applies them on
yaml.v3's node tree, verifies the result (ADR-036 §4: no node outside the
edit changes; read back by `parse-tree` at the target path, its content is
the original's with these edits applied) and writes it atomically, or
writes nothing. The contract is `da-guard edit`'s row in
docs/cli-reference.md; this module only sends the operations and reads the
answer (`da-guard.write/v1`).

Which da-guard, and how long it may run, is `_lib_parse_tree`'s one
resolution (`_lib_parse_tree._run`: an explicit argument, else the tool's
`use_da_guard` selection, else `$DA_GUARD_BINARY`, then `$PATH`). No usable
da-guard, a timeout or an answer that is not a `da-guard.write/v1` document
raise `_lib_parse_tree.ParseTreeError` (`BinaryMissing` for no binary), as
for every other da-guard run.

Answers
-------
`edit` returns the document for `written`, `unchanged` and `dry_run` (whose
`content` is the text that would be written). A refusal (`status`
`refused`, da-guard exit 1: nothing was written) raises `EditRefused`; a
caller error (`caller_error`, exit 2: the JSON this module sent, a path
outside the tree or missing, a failed write) raises `EditCallerError`. Both
carry the document (`.doc`), the path, and da-guard's `reason` and
`message`; `str()` of either says all three.

Values
------
Every value carries its type (`{"str": …}`, `{"int": …}`, …; the edit
contract). Two rules decide which:

* `node_value`: a value the tool READ (a parse-tree node) keeps the type it
  was read with — a `!!str` stays a string, an `!!int` an int. An `!!int` /
  `!!float` whose text is not canonical decimal (`010`, `1e3`), and any
  other tag (`!!timestamp`, …), cannot be sent as read: `ValueError`.
* `cli_value`: a value the user TYPED is an `int` / `float` only when its
  text is canonical decimal as the edit contract takes it (`80`, `-1`,
  `0.5`), otherwise a `str` (`70:critical`, `010`, `1e3`, `disable` — each
  written double-quoted).

Several files, all or nothing
-----------------------------
`edit_all` edits several files in one run with the guarantee a tool that
used to write several files had ("nothing is written unless every file
passes"): phase 1 runs `edit --dry-run` on every file, and if any is
refused nothing is written and `EditsRefused` names every refusal; phase 2
then edits each file for real. ⚠️ Phase 2 can still fail after some files
were written — only for a write error (a full disk, a permission changed in
between) or for a file another writer changed between phase 1 and its
write that the edit can no longer be applied to (da-guard's own
`changed_during_write` comparison covers a change during the write itself).
That failure is `PartialWrite`, which names the files already written, and
the tool must say which ones they are: they are NOT rolled back.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import _lib_parse_tree

SUBCOMMAND = "edit"
EMIT = "emit"
SCHEMA = "da-guard.write/v1"

WRITTEN = "written"
UNCHANGED = "unchanged"
DRY_RUN = "dry_run"
REFUSED = "refused"
CALLER_ERROR = "caller_error"
STATUSES = frozenset({WRITTEN, UNCHANGED, DRY_RUN, REFUSED, CALLER_ERROR})

#: The edit contract's canonical decimal forms (pkg/confedit value.go): an
#: `int` of at most 18 digits, a `float` of digits, '.', digits.
_CANONICAL_INT = re.compile(r"(0|-?[1-9][0-9]*)")
_CANONICAL_FLOAT = re.compile(r"-?(0|[1-9][0-9]*)\.[0-9]+")
_MAX_INT_DIGITS = 18


class EditError(Exception):
    """da-guard answered, and did not write: `.doc` is its document."""

    def __init__(self, doc: dict) -> None:
        self.doc = doc
        self.path = str(doc.get("path") or "")
        self.status = str(doc.get("status") or "")
        self.reason = str(doc.get("reason") or "")
        self.message = str(doc.get("message") or "")
        super().__init__(f"{self.path}: da-guard {doc.get('command') or SUBCOMMAND} "
                         f"{self.status} ({self.reason}): {self.message}")


class EditRefused(EditError):
    """`status: refused` (da-guard exit 1): the edit does not hold; nothing written."""


class EditCallerError(EditError):
    """`status: caller_error` (da-guard exit 2): what this tool sent, or the
    path it named, is wrong — or the write itself failed."""


class EditsRefused(Exception):
    """`edit_all` phase 1: at least one file refused; nothing was written.
    `.refusals` is every `EditRefused`, in the order given."""

    def __init__(self, refusals: "Sequence[EditRefused]") -> None:
        self.refusals = list(refusals)
        super().__init__("; ".join(str(r) for r in self.refusals))


class PartialWrite(Exception):
    """`edit_all` phase 2 failed on `.failed` (a path) after `.written`
    (paths, in order) were written; `.cause` is the error. The written
    files stay written."""

    def __init__(self, written: "Sequence[str]", failed: str, cause: BaseException) -> None:
        self.written = list(written)
        self.failed = failed
        self.cause = cause
        done = ", ".join(self.written) or "(none)"
        super().__init__(f"{failed}: {cause} — already written: {done}")


# ── operations and values ───────────────────────────────────────────

def set_op(path: "Sequence[str]", value: dict) -> dict:
    """`set` the key at *path* to *value* (a typed value)."""
    return {"op": "set", "path": [str(k) for k in path], "value": value}


def unset_op(path: "Sequence[str]") -> dict:
    """`unset` the key at *path* (not there: no-op)."""
    return {"op": "unset", "path": [str(k) for k in path]}


def ensure_map_op(path: "Sequence[str]") -> dict:
    """`ensure-map` *path* (each missing mapping on it created)."""
    return {"op": "ensure-map", "path": [str(k) for k in path]}


def str_value(text: str) -> dict:
    return {"str": str(text)}


def map_value(entries: "Iterable[tuple[str, dict]]") -> dict:
    """A mapping value, its keys in the order given."""
    return {"map": [[str(k), v] for k, v in entries]}


def is_canonical_int(text: str) -> bool:
    return (_CANONICAL_INT.fullmatch(text) is not None
            and len(text) <= _MAX_INT_DIGITS)


def is_canonical_float(text: str) -> bool:
    return _CANONICAL_FLOAT.fullmatch(text) is not None


def cli_value(text: str) -> dict:
    """A value the user typed: `int` / `float` when *text* is canonical
    decimal as the edit contract takes it, otherwise `str`."""
    text = str(text)
    if is_canonical_int(text):
        return {"int": text}
    if is_canonical_float(text):
        return {"float": text}
    return {"str": text}


def node_value(node: Any) -> dict:
    """A value the tool read (a parse-tree node), with the type it was read
    with. Raises ValueError for a scalar the edit contract cannot carry as
    read (a non-canonical `!!int` / `!!float`, any other tag); a mapping's
    keys are sent sorted (the parse-tree document does not keep the file's
    order, `_lib_parse_tree`)."""
    if isinstance(node, list):
        return {"seq": [node_value(n) for n in node]}
    if isinstance(node, dict) and not _lib_parse_tree.is_scalar(node):
        return map_value((k, node_value(v)) for k, v in sorted(node.items()))
    if not _lib_parse_tree.is_scalar(node):
        raise ValueError(f"not a parse-tree node: {node!r}")
    tag, text = node["tag"], node["text"]
    if tag == "!!str":
        value = node.get("value")
        return {"str": value if isinstance(value, str) else text}
    if tag == "!!int" and is_canonical_int(text):
        return {"int": text}
    if tag == "!!float" and is_canonical_float(text):
        return {"float": text}
    if tag == "!!bool" and isinstance(node.get("value"), bool):
        return {"bool": node["value"]}
    if tag == "!!null":
        return {"null": None}
    raise ValueError(f"the value {text!r} ({tag}) cannot be written back as it is "
                     f"written (the edit takes canonical decimal numbers only); "
                     f"respell it by hand first")


# ── running da-guard ────────────────────────────────────────────────

def _check(doc: dict) -> None:
    status = doc.get("status")
    if status not in STATUSES:
        raise ValueError(f"status {status!r} is none of {sorted(STATUSES)}")
    if status == DRY_RUN and not isinstance(doc.get("content"), str):
        raise ValueError("a dry_run document without its content")


def _send(command: str, root: "Path | str", rel: str, payload: dict, dry_run: bool,
          binary: Optional[str], timeout: Optional[float]) -> dict:
    args = [command, "--config-dir", str(root), "--path", rel, "--input", "-", "--json"]
    if dry_run:
        args.append("--dry-run")
    raw = json.dumps(payload).encode("utf-8")
    doc = _lib_parse_tree._run(args, binary, timeout, stdin=raw, check=_check,
                               what=command, schema=SCHEMA,
                               accept=frozenset({0, 1, 2}))
    if doc["status"] == REFUSED:
        raise EditRefused(doc)
    if doc["status"] == CALLER_ERROR:
        raise EditCallerError(doc)
    return doc


def edit(root: "Path | str", rel: str, ops: "Sequence[dict]", *, dry_run: bool = False,
         binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """`da-guard edit --config-dir <root> --path <rel> --json [--dry-run]`
    with ``{"ops": ops}``: the document (status `written`, `unchanged` or
    `dry_run`). Raises EditRefused / EditCallerError / ParseTreeError."""
    return _send(SUBCOMMAND, root, rel, {"ops": list(ops)}, dry_run, binary, timeout)


def emit(root: "Path | str", rel: str, content: dict, *, dry_run: bool = False,
         binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """`da-guard emit` of a NEW file at *rel* whose top level is *content*
    (a `map_value`); answers and raises as `edit`."""
    return _send(EMIT, root, rel, {"content": content}, dry_run, binary, timeout)


def edit_all(root: "Path | str", edits: "Sequence[tuple[str, Sequence[dict]]]", *,
             binary: Optional[str] = None, timeout: Optional[float] = None) -> "list[dict]":
    """Edit every `(rel, ops)` of *edits* or none (module docstring, "Several
    files"): the phase-2 documents, in order (a file phase 1 found
    `unchanged` is not edited again; its phase-1 document stands).

    Raises EditsRefused (phase 1 refused one or more: nothing written),
    EditCallerError or ParseTreeError from phase 1 (nothing written), and
    PartialWrite from phase 2."""
    planned = []
    refusals = []
    for rel, ops in edits:
        try:
            planned.append((rel, ops, edit(root, rel, ops, dry_run=True,
                                           binary=binary, timeout=timeout)))
        except EditRefused as exc:
            refusals.append(exc)
    if refusals:
        raise EditsRefused(refusals)
    done: "list[dict]" = []
    written: "list[str]" = []
    for rel, ops, preview in planned:
        if preview["status"] == UNCHANGED:
            done.append(preview)
            continue
        try:
            doc = edit(root, rel, ops, binary=binary, timeout=timeout)
        except (EditError, _lib_parse_tree.ParseTreeError) as exc:
            raise PartialWrite(written, rel, exc) from exc
        if doc["status"] == WRITTEN:
            written.append(rel)
        done.append(doc)
    return done
