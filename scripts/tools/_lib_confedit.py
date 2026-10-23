"""_lib_confedit.py — a Python tool's change to conf.d files, made by Go (ADR-036 §2).

ADR-036 §2: only Go writes conf.d. A Python tool that changes conf.d
(#3206) does not produce a file's text itself: it says what to change, and
`da-guard write` does it — an EDIT of an existing file (`set` / `unset` /
`ensure-map` operations on key paths, each value with its type, applied on
yaml.v3's node tree; ADR-036 §4: no node outside the edit changes), a
CREATE of a new file from a typed value (its comments are layout:
`comments()`, `head_comment`), or a MOVE of an existing file, byte for
byte. Every file is read back by `parse-tree` in the tree with every file
of the call in place, and written atomically, or nothing is. The contract
is `da-guard write`'s row in docs/cli-reference.md; this module only sends
the files and reads the answer (`da-guard.write/v3`).

One call is one batch (#3206 step 10c): every file goes to ONE da-guard run,
judged once, all or nothing (`write_all`; `edit_all` / `emit_all` are its
edit-only and create-only forms). `edit` / `emit` / `move` are a batch of
one.

Which da-guard, and how long it may run, is `_lib_parse_tree`'s one
resolution (`_lib_parse_tree._run`: an explicit argument, else the tool's
`use_da_guard` selection, else `$DA_GUARD_BINARY`, then `$PATH`). No usable
da-guard, a timeout or an answer that is not a `da-guard.write/v3` document
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
`write_all` (and `edit_all` / `emit_all`) send every file in one batch:
da-guard judges the tree with all of them in place (a move's source gone),
and if any is refused nothing is written
and `EditsRefused` names every refusal. A batch whose every file passes but
which would worsen the rest of the tree (a carrier it deselects, a tenant
declared twice, a new verdict anywhere: da-guard's `worsens_tree`) is
`TreeWorsened`, an `EditsRefused` naming each place. A da-guard older than
this tool's (one answering `da-guard.write/v1` or `/v2`, or one that does
not know the command) took nothing: `EditCallerError`, reason `da_guard_mismatch`
(also any other schema on a dry run). One answering a schema not known to
be older (a newer da-guard) may have written: on a real write that is
`PartialWrite`, every file of the batch maybe written. ⚠️ The write itself can still stop
part-way — a write error (a full disk, a permission changed), or a file
another writer changed between the judgement and its rename: a created
file is removed again by da-guard and a move put back, but an edited file
already renamed stays. That is `PartialWrite`, which names the files written, and the tool
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

#: The one da-guard command every change goes through (edit, create, move).
COMMAND = "write"
SUBCOMMAND = COMMAND
SCHEMA = "da-guard.write/v3"

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
#: The call's reason when the batch would worsen the tree.
WORSENS_TREE = "worsens_tree"

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


class TreeWorsened(EditsRefused):
    """The batch passes file by file but would worsen the tree (da-guard
    reason `worsens_tree`; nothing written): `.worsened` is da-guard's list
    (`kind`, `path`, `tenant`, `finding`, `message`), and `.refusals` names
    each as an `EditRefused` (reason `worsens_tree`), so a caller that lists
    refusals lists these too."""

    def __init__(self, worsened: "Sequence[dict]", command: str = SUBCOMMAND) -> None:
        self.worsened = list(worsened)
        super().__init__([EditRefused({
            "path": w.get("path") or (f"(tenant {w['tenant']})" if w.get("tenant") else "(the tree)"),
            "status": REFUSED, "reason": WORSENS_TREE,
            "message": f"{w.get('kind')}: {w.get('message')}"}, command) for w in self.worsened])


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


def map_value(entries: "Iterable[tuple]") -> dict:
    """A mapping value, its keys in the order given. An entry is `(key,
    value)`, or `(key, value, comments)` with *comments* from `comments()`
    (or None: none)."""
    out = []
    for entry in entries:
        k, v, *rest = entry
        if len(rest) > 1:
            raise ValueError(f"a map entry is (key, value) or (key, value, comments), not {entry!r}")
        out.append([str(k), v, rest[0]] if rest and rest[0] else [str(k), v])
    return {"map": out}


def comments(head: Optional[str] = None, line: Optional[str] = None) -> dict:
    """A map entry's comments (`map_value`'s third element): *head* above
    the key (several lines), *line* after `key:` (one line). Layout, not
    content: da-guard writes each line `# <line>` and never reads it back. A
    carriage return or another control character is da-guard's bad_input."""
    out = {}
    if head is not None:
        out["head"] = str(head)
    if line is not None:
        out["line"] = str(line)
    return out


# ── batch entries (`write_all`) ─────────────────────────────────────

def edit_entry(rel: str, ops: "Sequence[dict]") -> dict:
    """Edit the existing file *rel* with *ops*."""
    return {"path": rel, "ops": list(ops)}


def create_entry(rel: str, content: dict, head_comment: Optional[str] = None) -> dict:
    """A NEW file at *rel* whose top level is *content* (a `map_value`),
    *head_comment* (several lines, a comment) above it."""
    entry = {"path": rel, "create": content}
    if head_comment is not None:
        entry["head_comment"] = str(head_comment)
    return entry


def move_entry(rel: str, from_rel: str) -> dict:
    """Move the existing file *from_rel* to the new path *rel*, byte for
    byte; *from_rel* is gone after."""
    return {"path": rel, "from": from_rel}


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
    """One da-guard run of *files* (each an `edit_entry` / `create_entry` /
    `move_entry`):
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


def _touched(files: "list[dict]") -> "list[str]":
    """Every path the batch may change: each entry's path, and a move's
    source."""
    out = []
    for f in files:
        out.append(f["path"])
        if "from" in f:
            out.append(f["from"])
    return out


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
    except _lib_parse_tree.NotThisDocument as exc:
        # It answered as another da-guard than this tool's. An older one
        # (exc.older: it writes v1, or knows no such command) cannot have
        # taken this batch: nothing of it was written. A dry run writes
        # nothing either way. Any other one (a newer schema) may have
        # written: on a real write, every file may be in place.
        if exc.older or dry_run:
            raise EditCallerError({"path": "", "status": CALLER_ERROR, "reason": "da_guard_mismatch",
                                   "message": str(exc)}, command) from exc
        raise PartialWrite([], "", exc, maybe_written=_touched(files)) from exc
    except _lib_parse_tree.ParseTreeError as exc:
        if dry_run:
            raise
        raise PartialWrite([], "", exc, maybe_written=_touched(files)) from exc
    results = doc["files"]
    status = doc["status"]
    if status in (WRITTEN, UNCHANGED, DRY_RUN):
        return results
    written = [r["path"] for r in results if r["status"] == WRITTEN]
    # A move whose outcome is not known may have changed its source too.
    maybe = [p for r in results if r["status"] == MAYBE_WRITTEN
             for p in (r["path"], r.get("from")) if p]
    failed = next((r for r in results if r["status"] in (CALLER_ERROR, REFUSED)), None)
    if written or maybe:
        cause: EditError = (EditRefused(failed, command) if failed and failed["status"] == REFUSED
                            else EditCallerError(failed or doc, command))
        raise PartialWrite(written, failed["path"] if failed else "", cause, maybe_written=maybe)
    if status == REFUSED and doc.get("reason") == WORSENS_TREE:
        raise TreeWorsened(doc.get("worsened") or [], command)
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
    """`da-guard write --config-dir <root> --json [--dry-run]` editing the
    one file *rel* with *ops*: its result (status `written`, `unchanged` or
    `dry_run`). Raises EditRefused / EditCallerError / ParseTreeError."""
    return _one(COMMAND, root, edit_entry(rel, ops), dry_run, binary, timeout)


def emit(root: "Path | str", rel: str, content: dict, *, head_comment: Optional[str] = None,
         dry_run: bool = False, binary: Optional[str] = None,
         timeout: Optional[float] = None) -> dict:
    """`da-guard write` creating a NEW file at *rel* whose top level is
    *content* (a `map_value`), *head_comment* above it; answers and raises as
    `edit` (PartialWrite when the file may be left in place)."""
    return _one(COMMAND, root, create_entry(rel, content, head_comment), dry_run, binary, timeout)


def move(root: "Path | str", rel: str, from_rel: str, *, dry_run: bool = False,
         binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """`da-guard write` moving *from_rel* to the new path *rel*, byte for
    byte (the target first, as `move_entry` and every entry take it);
    answers and raises as `edit`."""
    return _one(COMMAND, root, move_entry(rel, from_rel), dry_run, binary, timeout)


def write_all(root: "Path | str", entries: "Sequence[dict]", *, dry_run: bool = False,
              binary: Optional[str] = None, timeout: Optional[float] = None) -> "list[dict]":
    """Every entry of *entries* (`edit_entry` / `create_entry` /
    `move_entry`) or none, in ONE da-guard run (module docstring, "Several
    files"): the files' results, in order.

    Raises EditsRefused (one or more refused: nothing written; TreeWorsened
    when the batch would worsen the tree), EditCallerError (nothing
    written), PartialWrite (the write stopped part-way, or da-guard gave no
    answer), ParseTreeError (dry run only)."""
    return _batch(COMMAND, root, list(entries), dry_run, binary, timeout)


def edit_all(root: "Path | str", edits: "Sequence[tuple[str, Sequence[dict]]]", *,
             dry_run: bool = False, binary: Optional[str] = None,
             timeout: Optional[float] = None) -> "list[dict]":
    """`write_all` of an edit per `(rel, ops)` of *edits*."""
    return write_all(root, [edit_entry(rel, ops) for rel, ops in edits],
                     dry_run=dry_run, binary=binary, timeout=timeout)


def emit_all(root: "Path | str", files: "Sequence[tuple[str, dict]]", *,
             dry_run: bool = False, binary: Optional[str] = None,
             timeout: Optional[float] = None) -> "list[dict]":
    """`write_all` of a NEW file per `(rel, content)` of *files* (da-guard
    removes the created files again when its write stops part-way:
    PartialWrite then names only the ones it could not remove, as maybe
    written)."""
    return write_all(root, [create_entry(rel, content) for rel, content in files],
                     dry_run=dry_run, binary=binary, timeout=timeout)
