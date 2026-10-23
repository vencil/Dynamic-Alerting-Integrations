"""_lib_confedit.py — a Python tool's change to conf.d files, made by Go (ADR-036 §2).

ADR-036 §2: only Go writes conf.d. A Python tool that changes an EXISTING
conf.d file (ADR-036 step 10b, #3206) does not produce the file's text
itself: it says what to change — `set` / `unset` / `ensure-map` operations
on key paths, each value with its type — and `da-guard edit` applies them on
yaml.v3's node tree, verifies the result (ADR-036 §4: no node outside the
edit changes; read back by `parse-tree` in the tree with every file of the
call in place, its content is the original's with these edits applied) and
writes it atomically, or writes nothing. A NEW file goes to `da-guard emit`
the same way. The contract is `da-guard edit`'s and `emit`'s rows in
docs/cli-reference.md; this module only sends the files and reads the
answer (`da-guard.write/v2`).

One call is one batch (#3206 step 10c): every file goes to ONE da-guard run,
judged once, all or nothing. `edit` / `emit` are a batch of one.

Which da-guard, and how long it may run, is `_lib_parse_tree`'s one
resolution (`_lib_parse_tree._run`: an explicit argument, else the tool's
`use_da_guard` selection, else `$DA_GUARD_BINARY`, then `$PATH`). No usable
da-guard, a timeout or an answer that is not a `da-guard.write/v2` document
raise `_lib_parse_tree.ParseTreeError` (`BinaryMissing` for no binary), as
for every other da-guard run — except where a real write may have begun
(`edit_all` / `emit_all`, below).

Answers
-------
`edit` / `emit` return the file's result for `written`, `unchanged` and
`dry_run` (whose `content` is the text that would be written). A refusal
(`status` `refused`, da-guard exit 1: nothing was written) raises
`EditRefused`; a caller error (`caller_error`, exit 2: the JSON this module
sent, a path outside the tree, missing or given twice, a failed write)
raises `EditCallerError`. Both carry the file's result (`.doc`; the call's
document when no file is to blame), the path, and da-guard's `reason` and
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
`edit_all` / `emit_all` send every file in one batch: da-guard judges the
tree with all of them in place, and if any is refused nothing is written
and `EditsRefused` names every refusal. ⚠️ The write itself can still stop
part-way — a write error (a full disk, a permission changed), or a file
another writer changed between the judgement and its rename: an emitted
file is removed again by da-guard, but an edited file already renamed
stays. That is `PartialWrite`, which names the files written, and the tool
must say which ones they are: they are NOT rolled back. When da-guard gave
no answer for a real write (it was killed, timed out, or wrote no document),
every file of the batch is named as one that MAY have been written.
⚠️ An interrupt of this process (Ctrl-C, SIGTERM) during the write reports
nothing: KeyboardInterrupt / SystemExit propagate as they are, and the files
written by then stay written.
"""
from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import _lib_parse_tree

SUBCOMMAND = "edit"
EMIT = "emit"
SCHEMA = "da-guard.write/v2"

WRITTEN = "written"
UNCHANGED = "unchanged"
DRY_RUN = "dry_run"
REFUSED = "refused"
CALLER_ERROR = "caller_error"
NOT_WRITTEN = "not_written"
MAYBE_WRITTEN = "maybe_written"
#: The call's statuses, and a file's (which adds the last two).
STATUSES = frozenset({WRITTEN, UNCHANGED, DRY_RUN, REFUSED, CALLER_ERROR})
FILE_STATUSES = STATUSES | {NOT_WRITTEN, MAYBE_WRITTEN}

#: The edit contract's canonical decimal forms (pkg/confedit value.go): an
#: `int` of at most 18 digits, a `float` of digits, '.', digits.
_CANONICAL_INT = re.compile(r"(0|-?[1-9][0-9]*)")
_CANONICAL_FLOAT = re.compile(r"-?(0|[1-9][0-9]*)\.[0-9]+")
_MAX_INT_DIGITS = 18


class EditError(Exception):
    """da-guard answered, and did not write: `.doc` is the file's result (the
    call's document when no file is to blame)."""

    def __init__(self, doc: dict, command: str = SUBCOMMAND) -> None:
        self.doc = doc
        self.command = command
        self.path = str(doc.get("path") or "")
        self.status = str(doc.get("status") or "")
        self.reason = str(doc.get("reason") or "")
        self.message = str(doc.get("message") or "")
        super().__init__(f"{self.path}: da-guard {command} "
                         f"{self.status} ({self.reason}): {self.message}")


class EditRefused(EditError):
    """A file `refused` (da-guard exit 1): the edit does not hold; nothing written."""


class EditCallerError(EditError):
    """`status: caller_error` (da-guard exit 2): what this tool sent, or the
    path it named, is wrong — or the write itself failed, nothing written."""


class EditsRefused(Exception):
    """A batch refused one or more files; nothing was written. `.refusals`
    is every `EditRefused`, in the order given."""

    def __init__(self, refusals: "Sequence[EditRefused]") -> None:
        self.refusals = list(refusals)
        super().__init__("; ".join(str(r) for r in self.refusals))


class PartialWrite(Exception):
    """A batch's write stopped on `.failed` (a path; "" when da-guard gave no
    answer) after `.written` (paths, in order) were written; `.cause` is the
    error. The written files stay written. `.maybe_written` holds the files
    whose write may have landed: an emitted file da-guard could not remove
    again, or every file of the batch when da-guard gave no answer
    (`ParseTreeError`: killed, timed out, no document)."""

    def __init__(self, written: "Sequence[str]", failed: str, cause: BaseException,
                 maybe_written: "Sequence[str]" = ()) -> None:
        self.written = list(written)
        self.failed = failed
        self.cause = cause
        self.maybe_written = list(maybe_written)
        done = ", ".join(self.written) or "(none)"
        maybe = (f"; may have been written: {', '.join(self.maybe_written)}"
                 if self.maybe_written else "")
        super().__init__(f"{failed or '(no answer)'}: {cause} — already written: {done}{maybe}")


# ── operations and values ───────────────────────────────────────────

def set_op(path: "Sequence[str]", value: dict) -> dict:
    """`set` the key at *path* to *value* (a typed value); an empty *path*
    sets the whole top level (*value* a `map_value`)."""
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
    files = doc.get("files")
    if not isinstance(files, list):
        raise ValueError("no files list")
    for f in files:
        if not isinstance(f, dict) or f.get("status") not in FILE_STATUSES:
            raise ValueError(f"a file result {f!r} with no status of {sorted(FILE_STATUSES)}")
        if f["status"] == DRY_RUN and not isinstance(f.get("content"), str):
            raise ValueError("a dry_run file without its content")


def _run(command: str, root: "Path | str", files: "list[dict]", dry_run: bool,
         binary: Optional[str], timeout: Optional[float]) -> dict:
    """One da-guard run of *files* (each `{"path": …, "ops"|"content": …}`):
    its document, the files answered in the order sent."""
    args = [command, "--config-dir", str(root), "--input", "-", "--json"]
    if dry_run:
        args.append("--dry-run")
    raw = json.dumps({"files": files}).encode("utf-8")
    doc = _lib_parse_tree._run(args, binary, timeout, stdin=raw, check=_check,
                               what=command, schema=SCHEMA,
                               accept=frozenset({0, 1, 2}))
    # da-guard answers each path cleaned ('/'); sent clean, it is the same.
    answered = [f.get("path") for f in doc["files"]]
    if doc["files"] and answered != [posixpath.normpath(str(f["path"])) for f in files]:
        raise _lib_parse_tree.ParseTreeError(
            f"da-guard {command} did not answer the files it was given, in order; "
            "nothing of it is used")
    return doc


def _batch(command: str, root: "Path | str", files: "list[dict]", dry_run: bool,
           binary: Optional[str], timeout: Optional[float]) -> "list[dict]":
    """Every file of *files* in one run, or none (module docstring): the
    files' results. Raises EditsRefused, EditCallerError, PartialWrite and
    — when nothing can have been written — ParseTreeError."""
    if not files:
        return []
    try:
        doc = _run(command, root, files, dry_run, binary, timeout)
    except _lib_parse_tree.BinaryMissing:
        raise  # nothing was run
    except _lib_parse_tree.ParseTreeError as exc:
        if dry_run:
            raise
        raise PartialWrite([], "", exc, maybe_written=[f["path"] for f in files]) from exc
    results = doc["files"]
    status = doc["status"]
    if status in (WRITTEN, UNCHANGED, DRY_RUN):
        return results
    written = [r["path"] for r in results if r["status"] == WRITTEN]
    maybe = [r["path"] for r in results if r["status"] == MAYBE_WRITTEN]
    failed = next((r for r in results if r["status"] in (CALLER_ERROR, REFUSED)), None)
    if written or maybe:
        cause: EditError = (EditRefused(failed, command) if failed and failed["status"] == REFUSED
                            else EditCallerError(failed or doc, command))
        raise PartialWrite(written, failed["path"] if failed else "", cause, maybe_written=maybe)
    if status == REFUSED:
        raise EditsRefused([EditRefused(r, command) for r in results if r["status"] == REFUSED])
    blamed = next((r for r in results if r["status"] == CALLER_ERROR), None)
    raise EditCallerError(blamed if blamed is not None else doc, command)


def _one(command: str, root: "Path | str", body: dict, dry_run: bool,
         binary: Optional[str], timeout: Optional[float]) -> dict:
    try:
        return _batch(command, root, [body], dry_run, binary, timeout)[0]
    except EditsRefused as exc:
        raise exc.refusals[0] from None
    except PartialWrite as exc:
        if isinstance(exc.cause, _lib_parse_tree.ParseTreeError):
            raise exc.cause from None  # no answer: as every other da-guard run
        raise


def edit(root: "Path | str", rel: str, ops: "Sequence[dict]", *, dry_run: bool = False,
         binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """`da-guard edit --config-dir <root> --json [--dry-run]` of the one file
    *rel* with *ops*: its result (status `written`, `unchanged` or
    `dry_run`). Raises EditRefused / EditCallerError / ParseTreeError."""
    return _one(SUBCOMMAND, root, {"path": rel, "ops": list(ops)}, dry_run, binary, timeout)


def emit(root: "Path | str", rel: str, content: dict, *, dry_run: bool = False,
         binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """`da-guard emit` of a NEW file at *rel* whose top level is *content*
    (a `map_value`); answers and raises as `edit` (PartialWrite when the
    file may be left in place)."""
    return _one(EMIT, root, {"path": rel, "content": content}, dry_run, binary, timeout)


def edit_all(root: "Path | str", edits: "Sequence[tuple[str, Sequence[dict]]]", *,
             dry_run: bool = False, binary: Optional[str] = None,
             timeout: Optional[float] = None) -> "list[dict]":
    """Edit every `(rel, ops)` of *edits* or none, in ONE da-guard run
    (module docstring, "Several files"): the files' results, in order.

    Raises EditsRefused (one or more refused: nothing written),
    EditCallerError (nothing written), PartialWrite (the write stopped
    part-way, or da-guard gave no answer), ParseTreeError (dry run only)."""
    return _batch(SUBCOMMAND, root, [{"path": rel, "ops": list(ops)} for rel, ops in edits],
                  dry_run, binary, timeout)


def emit_all(root: "Path | str", files: "Sequence[tuple[str, dict]]", *,
             dry_run: bool = False, binary: Optional[str] = None,
             timeout: Optional[float] = None) -> "list[dict]":
    """Write every NEW `(rel, content)` of *files* or none, in ONE da-guard
    run; answers and raises as `edit_all` (da-guard removes the emitted
    files again when its write stops part-way: PartialWrite then names only
    the ones it could not remove, as maybe written)."""
    return _batch(EMIT, root, [{"path": rel, "content": content} for rel, content in files],
                  dry_run, binary, timeout)
