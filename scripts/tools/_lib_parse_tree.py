"""_lib_parse_tree.py — read `da-guard parse-tree`'s document (ADR-036 §2).

`da-guard parse-tree --config-dir <dir>` writes, as one JSON document
(schema `da-guard.parse-tree/v2`), the files of a conf.d tree as yaml.v3
parses them: one entry per path the exporter's walk records — what the path
is (`role`, Go's verdict), who reads it, what stops a read (`problem`) — and
each readable file's content as a tree of tagged nodes. ADR-036 makes Go the
only YAML parser of conf.d; a Python tool that needs the content of a file
reads it from this document instead of parsing the YAML itself, and one that
needs "which files are there, and what is each" reads the entries instead of
judging paths on its own.

This module runs the subcommand and hands back the document, or raises. A
document it cannot read in full is unusable whole (ADR-036 §2): there is no
partial result.

Node shapes in ``files[<rel>]``: a mapping is a JSON object, a sequence is a
JSON array, and a scalar is ``{"text": <source text>, "tag": <resolved tag>,
"value": <str | bool | null>}`` — `is_scalar` tells a scalar from a mapping.

Used by `ops/migration_audit`, by `ops/config_diff`'s platform-settings
comparison (ADR-037 decision 3) and, through `_lib_parse_cache.ConfdReader`,
by the route generator and every tool that reads conf.d through it (ADR-036
step 4, #2906).

Loader API (ADR-036 step 4, #2906)
----------------------------------
`load` wraps `run`'s document in a read-only `Tree`; `Tree.value(rel)` is a
file's content as Python values (`to_python`), `Tree.node(rel)` the tagged
nodes for a reader that needs the source text (`scalar_text`). A file that
cannot be read raises `FileUnreadable` — never an empty mapping (ADR-036 §2:
unreadable is not empty) — and a file no reader selects raises `KeyError`.

`to_python` reads each scalar by its tag and nothing else: `!!str` is the
string (`yes` stays "yes"), `!!int` an exact `int`, `!!float` a `float`,
`!!bool` / `!!null` `True` / `False` / `None`, `!!timestamp` a `Timestamp`
(its text; not a `str`, not a `datetime`). It has no lenient conversion of
the kind yaml.v3 makes when it decodes into a typed Go field (ADR-036 §2).

Entries (ADR-036 §7 step 9, #3206)
----------------------------------
Every mode's document carries `entries`, read here into `Entry` objects:
`Tree.entries` for tree and file mode, `Listing.entries` for list mode
(`list_tree`, `parse-tree --list-only`, no content read) and
`classify_names` for names that are not on a disk. ⛔ An entry's `role` is
the verdict — `config` (a config file; its readers fail loudly on a
`problem`), `dir` (a directory the walk enters: every one has an entry, so
no reader lists directories itself; read by no reader, `readers` []),
`not_a_file`, `unlistable_dir` — and nothing here composes one of its own
from the other fields. `not_a_file` and `unlistable_dir` always carry a
`problem` (a document without one is refused); a `dir` carries one only
when its name is a config file's (`x.yaml/`). `Entry.flagged` is exactly
those three cases.

⛔ An entry's `verdict` is Go's answer to "what does this path make
`da-guard` validation do" — `unservable` (named under exit 3: parse_failed
or unreadable, the exporter cannot serve it), `tree_refused` (named under
exit 4 as unreadable_outside_run: the route generator refuses the whole
tree on it), or None. Go computes it from validation's own lists; a reader
that must fail closed on every path validation names reads this field and
composes nothing from role and problem (a file the exporter's decode
refuses can have no `problem` at all). ⚠️ Per mode: tree mode answers for
every entry of the tree on disk; file mode gives the full verdict for the
target path alone, once the text is written there (None for a path no walk
records) — read tree mode for the rest; list mode reads no byte, so it
knows only the walk's half of exit 3 — a file whose content the exporter
refuses is None there and `unservable` in tree mode: a reader that must
name every path validation names reads tree mode, not a listing. Names
mode: always None. A broken unselected `_defaults` carrier below the root is
both `not_read` unselected_defaults_carrier and `unservable`. ⛔ Per path
only: None on every entry is NOT "validation passes" — a tenant declared
twice (exit 2) or routing content (exit 4) falls on no path; a reader that
needs the whole verdict runs validation (`da-tools guard defaults-impact`).

"Which files can declare a tenant" is ONE answer with two halves
(`tenant_scope`): the tenant files, and the entries that leave the set
unknown (a directory Go could not list, a link it does not follow, …). A
reader of the tenant set takes both or neither. `Tree.selected` / `unreadable` / `not_read`
are computed from the entries, for the readers written against them.

A document of another schema (v1 included: a da-guard older than this tool),
or of this one with a field missing or of the wrong shape, is unusable whole.

A reader that holds a tree-mode document takes its listing from it
(`listing_of`): one snapshot for "which files" and "what is in them", never
a second walk that may see another tree. `list_tree` lists afresh on every
call (no memo); a reader that needs one snapshot for several questions keeps
the `Listing` and passes it down.

Which da-guard (#3206)
----------------------
Every run here — tree, file, list and names mode — resolves its binary and
timeout in one place, `_run`: an explicit *binary* / *timeout* argument,
else the one a tool selected for its run (`use_da_guard`, set once from its
`--da-guard-binary` / `--timeout` flags), else `$DA_GUARD_BINARY`, then
`$PATH`, and `DEFAULT_TIMEOUT`. So a tool's flags reach a listing a library
takes on its behalf as they reach its tree read.

⛔ KEY ORDER IS NOT THE FILE'S. parse-tree writes each mapping from a Go map,
and encoding/json writes a map's keys sorted (byte order). Measured on a file
written `zeta, alpha{mike, bravo, "10", "9"}, Mid, _b`: the document holds
`Mid, _b, alpha{10, 9, bravo, mike}, zeta`. The order of a `Tree.value`
mapping is that sorted order; a reader that needs the order the author wrote
cannot get it from this document.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import math
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, field as _field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

_HERE = Path(__file__).resolve().parent
# guard_dispatch lives in ops/ in the repo; the image ships it flat beside
# this module. Appended, so nothing there shadows a module already found.
if (_HERE / "ops").is_dir() and str(_HERE / "ops") not in sys.path:
    sys.path.append(str(_HERE / "ops"))

import guard_dispatch  # noqa: E402

SUBCOMMAND = "parse-tree"
SCHEMA = "da-guard.parse-tree/v2"
#: Schema values of documents a da-guard older than this tool writes: named
#: as such when refused, so the message says what to do (upgrade it).
_OLDER_SCHEMAS = frozenset({"da-guard.parse-tree/v1", "da-guard.parse-tree-list/v1"})
_OLDER = "this da-guard is older than this tool; upgrade or rebuild it"

#: The timeout of a da-guard run nobody named one for (seconds).
DEFAULT_TIMEOUT = 600

#: (binary, timeout) a tool selected for its run (`use_da_guard`); None for
#: either means "not selected". A ContextVar, so a selection ends with the
#: block that made it and no test or nested caller inherits a stale one.
_SELECTED: "contextvars.ContextVar[tuple[Optional[str], Optional[float]]]" = (
    contextvars.ContextVar("_lib_parse_tree._SELECTED", default=(None, None)))


@contextlib.contextmanager
def use_da_guard(binary: Optional[str] = None,
                 timeout: Optional[float] = None) -> Iterator[None]:
    """For the length of the block, every da-guard run that names no binary
    or timeout of its own uses *binary* / *timeout* — what a tool's
    `--da-guard-binary` / `--timeout` flags said. Set once, where the tool
    parses its arguments.

    A None INHERITS: the selection of an enclosing block if there is one,
    else the default resolution ($DA_GUARD_BINARY, then $PATH;
    `DEFAULT_TIMEOUT`) — a library entry point that passes its own None
    through never undoes its caller's choice.

    ⚠️ A ContextVar: a thread started inside the block does NOT inherit the
    selection (a new thread begins with an empty context unless it is run
    with `contextvars.copy_context().run`); such a thread resolves as if no
    block were open."""
    outer_binary, outer_timeout = _SELECTED.get()
    token = _SELECTED.set((binary if binary is not None else outer_binary,
                           timeout if timeout is not None else outer_timeout))
    try:
        yield
    finally:
        _SELECTED.reset(token)


def _resolve(binary: Optional[str], timeout: Optional[float]
             ) -> "tuple[Optional[str], float]":
    """(*binary*, *timeout*), each None replaced by the selection
    (`use_da_guard`), the timeout's last by `DEFAULT_TIMEOUT`."""
    sel_binary, sel_timeout = _SELECTED.get()
    binary = binary if binary is not None else sel_binary
    if timeout is None:
        timeout = sel_timeout if sel_timeout is not None else DEFAULT_TIMEOUT
    return binary, timeout

#: Set by :func:`run_under_memory_limit` in a process that limited its own
#: memory (the generator container's judgement, ADR-036 §3).
_MEMORY_LIMITED = False


def run_under_memory_limit() -> None:
    """Declare that this process runs under a memory limit (RLIMIT_DATA) that
    every da-guard it starts inherits. From then on each da-guard runs with
    ``GOTRACEBACK=crash``, so a Go runtime that fails — out of memory, an
    allocation failure that faults inside the runtime (SIGSEGV), a thread it
    cannot create — ends by a signal (SIGABRT) instead of exiting 2; and a
    da-guard ended by a signal is this process's memory limit, raised as
    ``MemoryError`` naming the signal. Judged by the process status, not by
    its words. Without this call a da-guard ended by a signal stays a
    ``ParseTreeError``. The caller sets RLIMIT_CORE to 0: no core file."""
    global _MEMORY_LIMITED
    _MEMORY_LIMITED = True


class ParseTreeError(Exception):
    """parse-tree gave no document this module can read; the message says why."""


class BinaryMissing(ParseTreeError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class FileUnreadable(ParseTreeError):
    """A selected file parse-tree could not read: `kind` is its closed-list code
    (pkg/parsetree Kinds), `message` the reason for a person."""

    def __init__(self, file: str, kind: str, message: str) -> None:
        super().__init__(f"{file}: {kind}: {message}")
        self.file = file
        self.kind = kind
        self.message = message


def run(root: Path | str, binary: Optional[str] = None,
        timeout: Optional[float] = None) -> dict:
    """`da-guard parse-tree --config-dir <root>`'s document. Raises
    `BinaryMissing` when there is no binary, `ParseTreeError` when it fails or
    its output is not a parse-tree document this reader reads."""
    return _run([SUBCOMMAND, "--config-dir", str(root)], binary, timeout)


def run_file(root: Path | str, rel: str, text: bytes,
             binary: Optional[str] = None, timeout: Optional[float] = None) -> dict:
    """File mode: `da-guard parse-tree --config-dir <root> --path <rel>
    --input -` with *text* on stdin — *text* read as the file at *rel*
    (root-relative, '/'), in the tree as it is once that file is written.
    Raises as `run` does."""
    return _run([SUBCOMMAND, "--config-dir", str(root), "--path", rel,
                 "--input", "-"], binary, timeout, stdin=text)


def absolute_da_guard_binary(environ: "dict[str, str] | None" = None) -> None:
    """Make `$DA_GUARD_BINARY` in *environ* (default: this process's) an
    absolute path, against the current working directory — the meaning a
    relative path has in the shell that set it. Call it before changing
    directory, or before starting a process with another `cwd`, so the
    binary the user named is the one that runs. Unset or blank: unchanged."""
    env = os.environ if environ is None else environ
    value = env.get("DA_GUARD_BINARY", "")
    if value.strip() and not os.path.isabs(value):
        env["DA_GUARD_BINARY"] = os.path.abspath(value)


def _run(args: list[str], binary: Optional[str], timeout: Optional[float],
         stdin: Optional[bytes] = None, *,
         check: "Callable[[dict], None] | None" = None,
         what: str = SUBCOMMAND) -> dict:
    """Run `da-guard <args>` and return its document, checked whole by
    *check* (default `check_shape`). *what* names the command in every
    message — a flag a da-guard older than this tool lacks included (a
    binary that knows `parse-tree` but not `--list-only` is named as lacking
    `parse-tree --list-only`)."""
    check = check_shape if check is None else check
    binary, timeout = _resolve(binary, timeout)  # ⛔ the one place (#3206)
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is not None:
        # Resolved now, against the current directory (a relative
        # `$DA_GUARD_BINARY`, an explicit path, a relative `$PATH` entry).
        exe = os.path.abspath(exe)
    if exe is None:
        msg = dispatcher.binary_missing_message(binary).strip()
        for prefix in ("Error: ", "錯誤: "):
            msg = msg.removeprefix(prefix)
        raise BinaryMissing(msg)
    cmd = [exe, *args]
    env = {**os.environ, "GOTRACEBACK": "crash"} if _MEMORY_LIMITED else None
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout,
                              input=stdin if stdin is not None else b"", env=env)
    except subprocess.TimeoutExpired as e:
        raise ParseTreeError(f"da-guard {what} did not finish within {timeout}s") from e
    except OSError as e:
        raise ParseTreeError(f"da-guard could not be run ({exe}): {e}") from e
    stderr = proc.stderr.decode("utf-8", errors="backslashreplace").strip()
    if _MEMORY_LIMITED and proc.returncode < 0:
        # Ended by a signal under the memory limit it inherited (the
        # generator container's RLIMIT_DATA of a judgement, ADR-036 §3):
        # the caller's memory limit, not a da-guard defect.
        raise MemoryError(f"da-guard {what} was ended by "
                          f"{_signal_name(-proc.returncode)} under this process's "
                          f"memory limit (RLIMIT_DATA)")
    if proc.returncode != 0:
        older = ""
        if proc.returncode == 2 and not _accepts(cmd, timeout):
            older = (f" — this da-guard does not know `{what}`: it is older "
                     "than this tool; upgrade or rebuild it")
        raise ParseTreeError(f"da-guard {what} exited {proc.returncode}{older}"
                             + (f": {' '.join(stderr.split())}" if stderr else ""))
    return read_document(proc.stdout, check=check, what=what)


def read_document(raw: bytes, *, check: "Callable[[dict], None] | None" = None,
                  what: str = SUBCOMMAND) -> dict:
    """*raw* (parse-tree's stdout) as a document *check* accepts (default
    `check_shape`: tree or file mode). Raises ParseTreeError — naming a
    da-guard older than this tool as such — for anything else: a document
    wrong anywhere is unusable whole (ADR-036 §2)."""
    check = check_shape if check is None else check
    try:
        doc = json.loads(raw.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            got = doc.get("schema") if isinstance(doc, dict) else None
            hint = f" — {_OLDER}" if got in _OLDER_SCHEMAS else ""
            raise ValueError(f"schema {got!r}, this reader reads {SCHEMA!r}{hint}")
        check(doc)
    except (ValueError, TypeError, KeyError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        # ADR-036 §2: a document a reader cannot decode is unusable whole.
        raise ParseTreeError(f"da-guard {what} wrote no document this tool can "
                             f"read ({e}); nothing of it is used") from e
    return doc


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


# ── entries (ADR-036 §7 step 9, #3206) ──────────────────────────────

#: `role` values, a closed set (pkg/config Role): Go's verdict on a path.
ROLES = frozenset({"config", "dir", "not_a_file", "unlistable_dir"})
#: `class` values: of a walked path, and the more a name alone can be.
WALK_CLASSES = frozenset({"tenant", "defaults", "platform", "directory"})
NAME_CLASSES = frozenset({"tenant", "defaults", "platform", "hidden", "not_config_name"})
#: The problem kinds decided without reading a byte (list mode's only ones).
LIST_PROBLEM_KINDS = frozenset({"read_failure", "not_regular_file"})
#: Classes whose name is `_`-prefixed: a platform file, read for no tenant.
RESERVED_CLASSES = frozenset({"defaults", "platform"})
#: `verdict` values, a closed set (cmd/da-guard verdict.go); None = nothing.
VERDICTS = frozenset({"unservable", "tree_refused"})
#: Every field an entry carries, in every mode (null where it has no value).
ENTRY_FIELDS = ("path", "depth", "class", "tenant_named", "defaults_document",
                "readers", "not_read", "role", "problem", "verdict")
MODES = frozenset({"tree", "file", "list", "names"})


@dataclass(frozen=True)
class Entry:
    """One entry of a parse-tree document. *path* is root-relative with '/'
    ("." for the root itself; in names mode the name as given); *cls* the
    closed-set class; *tenant_named* the id the name carries (class tenant
    only); *defaults_document* whether it is a platform-defaults document;
    *readers* who reads it (None in names mode); *not_read* why nobody does;
    *role* Go's verdict on what the path is (None in names mode); *problem*
    ``(kind, message)`` when a read of it fails; *verdict* what the path makes
    da-guard validation do — `unservable` (exit 3), `tree_refused` (exit 4),
    None — as Go computes it from validation's own lists (see the module
    docstring for what list, file and names mode can know).

    ⚠️ In file mode *role* says only whether the path on disk holds a
    regular file the walk can reach (or nothing yet) — not that its name is
    a config file name: a `--path` of `x.txt` or `.git/x.yaml` is `config`
    with *not_read* saying no reader reads it."""
    path: str
    depth: int
    cls: str
    tenant_named: Optional[str]
    defaults_document: bool
    readers: Optional[tuple[str, ...]]
    not_read: Optional[str]
    role: Optional[str]
    problem: Optional[tuple[str, str]]
    verdict: Optional[str]

    @property
    def name(self) -> str:
        """The entry's base name."""
        return self.path.rsplit("/", 1)[-1]

    @property
    def config_file(self) -> bool:
        """A config file (role `config`) — readable or not: a `problem` on one
        is a read that fails, loudly, never a reason to skip it."""
        return self.role == "config"

    @property
    def is_dir(self) -> bool:
        """A directory the walk enters and lists (role `dir`). One whose name
        is a config file name (`x.yaml/`) carries a `problem` saying so; its
        files are read like any directory's."""
        return self.role == "dir"

    @property
    def flagged(self) -> bool:
        """What every reader names instead of passing over: role
        `not_a_file` (a symlink to a directory the walk does not follow, a
        special file, a dangling link), role `unlistable_dir` (a directory it
        could not list) — each always carries a `problem` (`_entry_of`
        refuses one without) — and role `dir` WITH a problem (a directory
        with a file's name; a `dir` without one is a plain directory). The
        fact is Go's role and problem; this composes nothing beyond them."""
        return (self.role in ("not_a_file", "unlistable_dir")
                or (self.role == "dir" and self.problem is not None))

    @property
    def dir_link(self) -> bool:
        """A link to a directory the walk does not follow: role `not_a_file`
        of class `directory` (Go's two fields; no stat here). What is under
        it is not loaded."""
        return self.role == "not_a_file" and self.cls == "directory"

    @property
    def not_a_file(self) -> bool:
        """No regular file the exporter's walk can reach (role `not_a_file`)."""
        return self.role == "not_a_file"

    @property
    def unlistable_dir(self) -> bool:
        """A directory the walk could not list (role `unlistable_dir`): the
        document is incomplete below it."""
        return self.role == "unlistable_dir"

    @property
    def reserved(self) -> bool:
        """A `_`-prefixed config name (class defaults or platform)."""
        return self.cls in RESERVED_CLASSES

    @property
    def root_platform_file(self) -> bool:
        """A root platform file (#2743): a `_`-prefixed config file at the
        conf.d root that the exporter reads — Go's `isRootPlatformKey`
        (flat_build.go), whose `tenants:` block is the platform's per-tenant
        default (platform_overlay.go). Go's three fields, composed: class
        (reserved), depth 0, `exporter` among the readers (an unselected
        carrier has none; parse_tree.go `readersFor`). False in names mode."""
        return (self.reserved and self.depth == 0
                and self.readers is not None and "exporter" in self.readers)


def _entry_of(e: Any, i: int, mode: str) -> Entry:
    """One raw entry, checked whole. A field absent from it is a da-guard
    older than this tool (every field is always written), named as such."""
    if not isinstance(e, dict):
        raise ValueError(f"entries[{i}] is not an object")
    path = e.get("path")
    where = f"entries[{i}] ({path!r})"
    missing = [f for f in ENTRY_FIELDS if f not in e]
    if missing:
        raise ValueError(f"{where}: no {', '.join(missing)} — {_OLDER}")
    if set(e) - set(ENTRY_FIELDS):
        raise ValueError(f"{where}: fields this reader does not know: {sorted(set(e) - set(ENTRY_FIELDS))}")
    depth, cls, tenant = e["depth"], e["class"], e["tenant_named"]
    names = mode == "names"
    if not isinstance(path, str) or not path:
        raise ValueError(f"entries[{i}] has no path")
    if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
        raise ValueError(f"{where}: depth is not a count")
    classes = {"tree": WALK_CLASSES, "list": WALK_CLASSES, "names": NAME_CLASSES,
               "file": WALK_CLASSES | NAME_CLASSES}[mode]
    if cls not in classes:
        raise ValueError(f"{where}: class {cls!r} is not one this reader knows in {mode} mode")
    if tenant is not None and not (cls == "tenant" and isinstance(tenant, str) and tenant):
        raise ValueError(f"{where}: tenant_named does not match class {cls!r}")
    if not isinstance(e["defaults_document"], bool):
        raise ValueError(f"{where}: defaults_document is not a boolean")
    readers, role, not_read, bad = e["readers"], e["role"], e["not_read"], e["problem"]
    verdict = e["verdict"]
    if verdict is not None and verdict not in VERDICTS:
        raise ValueError(f"{where}: verdict {verdict!r} is not one this reader knows")
    if verdict == "tree_refused" and bad is None:
        # Go names a path tree_refused only for its problem.
        raise ValueError(f"{where}: verdict tree_refused with no problem")
    if names:
        if readers is not None or role is not None or not_read is not None or bad is not None \
                or verdict is not None:
            raise ValueError(f"{where}: readers, role, not_read, problem or verdict in names mode")
    else:
        if not (isinstance(readers, list) and all(isinstance(r, str) for r in readers)):
            raise ValueError(f"{where}: readers is not a list of reader names")
        if role not in ROLES:
            raise ValueError(f"{where}: role {role!r} is not one this reader knows")
        if not_read is not None and not (isinstance(not_read, str) and not_read):
            raise ValueError(f"{where}: not_read is not a reason")
        if role in ("dir", "unlistable_dir"):
            # A directory, listed or not, is no file a reader reads, and
            # nothing is passed over.
            if readers or not_read is not None:
                raise ValueError(f"{where}: a {role} with readers {readers!r} / "
                                 f"not_read {not_read!r}")
        elif (not_read is None) == (not readers):
            raise ValueError(f"{where}: readers {readers!r} with not_read {not_read!r}")
    if role in ("not_a_file", "unlistable_dir") and bad is None:
        # Go always says why a path is no file it reads: one without the why
        # is a document this reader does not understand.
        raise ValueError(f"{where}: role {role} with no problem")
    problem = None
    if bad is not None:
        kind = bad.get("kind") if isinstance(bad, dict) else None
        kinds_ok = kind in LIST_PROBLEM_KINDS if mode == "list" else isinstance(kind, str) and bool(kind)
        if not (kinds_ok and set(bad) == {"kind", "message"} and isinstance(bad.get("message"), str)):
            raise ValueError(f"{where}: problem is not {{kind, message}} of a kind {mode} mode gives")
        problem = (bad["kind"], bad["message"])
    return Entry(path=path, depth=depth, cls=cls, tenant_named=tenant,
                 defaults_document=e["defaults_document"],
                 readers=None if readers is None else tuple(readers),
                 not_read=not_read, role=role, problem=problem, verdict=verdict)


def entries_of(doc: dict) -> "tuple[Entry, ...]":
    """The `entries` of a document `check_shape` (or a list check) accepted,
    as `Entry` objects. Raises ValueError for one it would not accept."""
    mode = doc.get("mode")
    if mode not in MODES:
        raise ValueError(f"mode {mode!r} is not one this reader knows")
    raw = doc.get("entries")
    if not isinstance(raw, list):
        raise ValueError(f"field 'entries' missing or not a list — {_OLDER}")
    out = tuple(_entry_of(e, i, mode) for i, e in enumerate(raw))
    if mode != "names" and len({e.path for e in out}) != len(out):
        raise ValueError("a path is listed twice")
    return out


def _check_index(doc: dict, modes: "frozenset[str]") -> "tuple[Entry, ...]":
    for field in ("mode", "da_guard_version"):
        if field not in doc:
            raise ValueError(f"no {field!r} — {_OLDER}")
    if not isinstance(doc["da_guard_version"], str):
        raise ValueError("da_guard_version is not a string")
    if doc.get("mode") not in modes:
        raise ValueError(f"mode {doc.get('mode')!r}, this reader asked for {' or '.join(sorted(modes))}")
    dc = doc.get("defaults_carriers")
    if not (isinstance(dc, dict) and all(isinstance(v, str) for v in dc.values())):
        raise ValueError("defaults_carriers is not {dir: path}")
    return entries_of(doc)


def check_shape(doc: dict) -> None:
    """Raise ValueError unless `doc` is a tree- or file-mode document of the
    shape this module reads, every element of it: a document that is wrong
    anywhere is unusable whole."""
    entries = _check_index(doc, frozenset({"tree", "file"}))
    for field in ("files", "rendered_by"):
        if not isinstance(doc.get(field), dict):
            raise ValueError(f"field {field!r} missing or not an object — {_OLDER}"
                             if field not in doc else f"field {field!r} is not an object")
    for f, content in doc["files"].items():
        if not isinstance(content, dict) or is_scalar(content):
            raise ValueError(f"files[{f!r}] is not a mapping")
        stack: list[Any] = [content]
        while stack:
            v = stack.pop()
            if isinstance(v, list):
                stack.extend(v)
            elif is_scalar(v):
                if not (isinstance(v["tag"], str) and isinstance(v.get("value"), (str, bool, type(None)))):
                    raise ValueError(f"files[{f!r}] holds a scalar of the wrong shape: {v!r:.80}")
            elif isinstance(v, dict) and not isinstance(v.get("text"), str):  # a mapping
                stack.extend(v.values())
            else:
                raise ValueError(f"files[{f!r}] holds a value of the wrong shape: {v!r:.80}")
    for f, keys in doc["rendered_by"].items():
        if not (isinstance(keys, dict) and all(
                isinstance(rs, list) and all(isinstance(r, str) for r in rs) for rs in keys.values())):
            raise ValueError(f"rendered_by[{f!r}] is not {{key: [reader]}}")
    # parse-tree's own invariants: content belongs to an entry, never beside
    # a problem; tree mode reads every config file a reader reads.
    by_path = {e.path: e for e in entries}
    for f in doc["files"]:
        if f not in by_path:
            raise ValueError(f"files[{f!r}] is no entry's")
        if by_path[f].problem is not None:
            raise ValueError(f"{f!r} has content and a problem")
        if doc["mode"] == "tree" and not by_path[f].config_file:
            raise ValueError(f"{f!r} has content but role {by_path[f].role!r}")
    if doc["mode"] == "file" and len(entries) != 1:
        raise ValueError("a file-mode document holds one entry")
    for e in entries:
        if doc["mode"] == "tree" and e.config_file and e.readers and e.problem is None \
                and e.path not in doc["files"]:
            raise ValueError(f"{e.path!r}: a config file a reader reads, with neither content nor a problem")
        # A directory --path (role dir / unlistable_dir: the walk's own entry)
        # is no file: file mode reads no text for it, and that is no gap.
        if doc["mode"] == "file" and e.problem is None and e.path not in doc["files"] \
                and e.role not in ("dir", "unlistable_dir"):
            raise ValueError(f"{e.path!r}: file mode gave neither content nor a problem")
        if doc["mode"] == "file" and e.role in ("dir", "unlistable_dir") and e.path in doc["files"]:
            raise ValueError(f"{e.path!r}: file mode read text for a directory")


def is_scalar(v: Any) -> bool:
    """Whether a node is a scalar: the only object whose "text" is a JSON string."""
    return isinstance(v, dict) and isinstance(v.get("text"), str) and "tag" in v


def _accepts(cmd: list[str], timeout: float) -> bool:
    """Whether the binary knows the subcommand and its flags (`-h` exits 0)."""
    try:
        proc = subprocess.run([*cmd, "-h"], capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# ── loader API (ADR-036 step 4, #2906) ──────────────────────────────

@dataclass(frozen=True, repr=False)
class Timestamp:
    """A `!!timestamp` scalar: only its source text. Not a `str` — yaml.v3 and
    the exporter do not read it as one — and not a `datetime`: what instant it
    names is no reader's question here. Its repr is the text as written, so a
    message reads ``got Timestamp 2001-12-15``."""
    text: str

    def __repr__(self) -> str:
        return self.text


# yaml.v3's spellings of the !!float values parse-tree gives no "value" for
# (third_party/yaml.v3/resolve.go, resolveMapList): an explicit `!!float`
# goes through the same table, any other spelling of infinity is not a float.
_FLOAT_WORDS = {
    **dict.fromkeys((".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"), math.inf),
    **dict.fromkeys(("-.inf", "-.Inf", "-.INF"), -math.inf),
    **dict.fromkeys((".nan", ".NaN", ".NAN"), math.nan),
}
_SCALAR_KEYS = frozenset({"text", "tag", "value"})
# The decimal text parse-tree writes (strconv FormatInt / FormatUint, and
# FormatFloat 'f' -1). Matched before converting: Python's int() and float()
# also take "+5", " 5", "1_000", Unicode digits and "nan" / "inf".
_INT_TEXT = re.compile(r"-?[0-9]+")
_FLOAT_TEXT = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _scalar(node: dict) -> Any:
    if not _SCALAR_KEYS.issuperset(node):
        raise ValueError(f"a scalar with fields beyond text, tag, value: {node!r:.80}")
    tag, text = node["tag"], node["text"]
    has, value = "value" in node, node.get("value")
    if tag == "!!str" and has and isinstance(value, str):
        return value
    if tag == "!!bool" and has and isinstance(value, bool):
        return value
    if tag == "!!null" and has and value is None:
        return None
    if tag == "!!int" and has and isinstance(value, str) and _INT_TEXT.fullmatch(value):
        return int(value, 10)
    if tag == "!!float" and has and isinstance(value, str) and _FLOAT_TEXT.fullmatch(value):
        return float(value)
    if tag == "!!float" and not has and text in _FLOAT_WORDS:
        return _FLOAT_WORDS[text]
    if tag == "!!timestamp" and not has:
        return Timestamp(text)
    raise ValueError(f"a scalar this reader does not read: {node!r:.80}")


def _new(node: Any) -> tuple[Any, bool]:
    """(the Python value of `node`, whether it is a container to fill)."""
    if is_scalar(node):
        return _scalar(node), False
    if isinstance(node, dict):
        return {}, True
    if isinstance(node, list):
        return [], True
    raise ValueError(f"not a parse-tree node: {node!r:.80}")


def to_python(node: Any, *, text_sequences: "frozenset[str]" = frozenset()) -> Any:
    """A parse-tree node as Python values: a mapping is a `dict` keyed by the
    JSON keys as written (each key's source text), a sequence a `list`, a
    scalar by its tag (see the module docstring). Any other tag or shape
    raises ValueError — `check_shape` refuses those first; this is the second
    line. Iterative: a document deep enough to decode is deep enough to
    convert. *text_sequences*: see `to_python_text_sequences`."""
    out, fill = _new(node)
    stack = [(node, out, False)] if fill else []
    while stack:
        src, dst, as_text = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, child in items:
            if as_text and is_scalar(child):
                v, more = scalar_text(child), False
            else:
                v, more = _new(child)
            if isinstance(dst, dict):
                dst[k] = v
            else:
                dst.append(v)
            if more:
                stack.append((child, v, isinstance(dst, dict) and k in text_sequences
                              and isinstance(child, list)))
    return out


def scalar_text(node: Any) -> str:
    """A scalar node's source text — what the exporter reads a threshold from."""
    if not is_scalar(node):
        raise ValueError(f"not a scalar node: {node!r:.80}")
    return node["text"]


@dataclass(frozen=True)
class Tree:
    """One tree- or file-mode parse-tree document, read-only. `entries`: the
    document's entries (`Entry`), sorted by path. Computed from them, for the
    readers written against these: `selected`, every path a reader tries
    (content or a problem) → its readers; `unreadable`, path → (kind,
    message) of every entry with a problem; `not_read`, path → why no reader
    reads it. `rendered_by`: file → key → readers. `defaults_carriers`:
    directory ("." for the root) → the one defaults carrier its chain reads."""
    selected: Mapping[str, tuple[str, ...]]
    unreadable: Mapping[str, tuple[str, str]]
    not_read: Mapping[str, str]
    rendered_by: Optional[Mapping[str, Mapping[str, tuple[str, ...]]]]
    _files: Mapping[str, Any]
    defaults_carriers: Optional[Mapping[str, str]] = None
    entries: "tuple[Entry, ...]" = ()

    def node(self, rel: str) -> Any:
        """File `rel`'s content as parse-tree's tagged nodes (shared with this
        Tree: do not mutate it). Raises FileUnreadable when parse-tree could
        not read it, KeyError when no reader selects it."""
        if rel in self.unreadable:
            kind, message = self.unreadable[rel]
            raise FileUnreadable(rel, kind, message)
        if rel not in self.selected:
            why = f" (not read: {self.not_read[rel]})" if rel in self.not_read else ""
            raise KeyError(f"{rel}: no reader selects this file{why}")
        return self._files[rel]

    def value(self, rel: str) -> dict:
        """File `rel`'s content as Python values (`to_python`), a new object on
        every call. Raises as `node` does."""
        return to_python(self.node(rel))


def tree_of(doc: dict) -> Tree:
    """A `Tree` of a document. Raises ParseTreeError for one `check_shape`
    refuses: a document wrong anywhere is unusable whole."""
    try:
        check_shape(doc)
        entries = entries_of(doc)
    except (ValueError, TypeError, KeyError) as e:
        raise ParseTreeError(f"da-guard {SUBCOMMAND} wrote no document this tool can "
                             f"read ({e}); nothing of it is used") from e
    files = doc["files"]
    selected = {e.path: e.readers for e in entries if e.path in files or e.problem is not None}
    return Tree(
        selected=MappingProxyType(selected),
        unreadable=MappingProxyType({e.path: e.problem for e in entries if e.problem is not None}),
        not_read=MappingProxyType({e.path: e.not_read for e in entries if e.not_read is not None}),
        rendered_by=MappingProxyType({f: MappingProxyType({k: tuple(rs) for k, rs in keys.items()})
                                      for f, keys in doc["rendered_by"].items()}),
        _files=MappingProxyType(dict(files)),
        defaults_carriers=MappingProxyType(dict(doc["defaults_carriers"])),
        entries=entries,
    )


def load(root: Path | str, binary: Optional[str] = None,
         timeout: Optional[float] = None) -> Tree:
    """`run`'s document as a `Tree`. Raises as `run` does, and as `tree_of`."""
    return tree_of(run(root, binary, timeout))


def load_file(root: Path | str, rel: str, text: bytes,
              binary: Optional[str] = None, timeout: Optional[float] = None) -> Tree:
    """`run_file`'s document as a `Tree` (it holds the one file *rel*).
    Raises as `run_file` does, and as `tree_of`."""
    return tree_of(run_file(root, rel, text, binary, timeout))


def to_python_text_sequences(node: Any, keys: "frozenset[str] | tuple[str, ...]") -> Any:
    """`to_python`, except that every scalar item of a sequence that is the
    value of a mapping key in *keys* is that item's source text
    (`scalar_text`): a domain policy's `tenants: [010]` is ["010"], the text
    the exporter keys tenant 010 by, never the integer 8. Any other item of
    such a sequence is converted as `to_python` converts it."""
    return to_python(node, text_sequences=frozenset(keys))


# ── the tenant set's scope (#3206) ──────────────────────────────────

@dataclass(frozen=True)
class TenantScope:
    """Where a tree's tenants can be declared, as ONE answer.

    *files*: every tenant file (role `config`, class `tenant`, read), sorted,
    root-relative — readable or not (a reader names one it cannot read).
    *unknown*: every entry that may be — or hide — a tenant file, as a
    `FileUnreadable` with Go's problem: a directory Go could not list and a
    link to a directory the walk does not follow, whatever their names (what
    is under them is unknown / not loaded); any other flagged entry (a
    dangling link, a special file, a directory with a config file's name)
    unless Go classes its name a platform (`_`) one, read for no tenant.

    ⛔ The halves are one answer: a tenant set computed from *files* alone
    over a tree with *unknown* entries is a set that was not measured. Every
    reader of "which tenants does this tree declare" takes this, never a
    file list of its own."""
    files: "tuple[str, ...]"
    unknown: "tuple[FileUnreadable, ...]"


def tenant_file_paths(entries: "Iterable[Entry]") -> "tuple[str, ...]":
    """`TenantScope.files` alone: role `config`, class `tenant`, read; sorted.
    ⛔ Half an answer — see `TenantScope`; only `tenant_scope` and the
    callers `_lib_parse_cache.tenant_files` names use it."""
    return tuple(sorted(e.path for e in entries
                        if e.config_file and e.cls == "tenant" and e.readers))


def tenant_scope(entries: "Iterable[Entry]", binary: Optional[str] = None,
                 timeout: Optional[float] = None, *,
                 exclude: "Optional[Callable[[str], bool]]" = None) -> TenantScope:
    """The `TenantScope` of a listing's or a tree's *entries* (one snapshot).
    *exclude*: a root-relative path to leave out of both halves. A flagged
    directory's name is classed by Go (`classify_names`, one names run for
    all of them; *binary* / *timeout* as there). Raises ParseTreeError."""
    entries = [e for e in entries if exclude is None or not exclude(e.path)]
    flagged = [e for e in entries if e.flagged]
    dirs = sorted({e.name for e in flagged if e.is_dir})
    named = classify_names(dirs, binary, timeout) if dirs else {}
    files = tenant_file_paths(entries)
    unknown = []
    for e in flagged:
        if e.unlistable_dir or e.dir_link:
            hides = True
        elif e.is_dir:
            hides = not named[e.name].reserved
        else:
            hides = not e.reserved
        if hides:
            unknown.append(FileUnreadable(e.path, *e.problem))
    return TenantScope(files, tuple(unknown))


# ── list API: `parse-tree --list-only` (ADR-036 §7 step 9, #3206) ───────

@dataclass(frozen=True)
class Listing:
    """One list-mode document. *root* is the directory as the caller named
    it; *entries* are sorted by path (the walk's order), one per path;
    *defaults_carriers* maps a directory ("." for the root) to the one
    defaults carrier its chain reads."""
    root: Path
    entries: "tuple[Entry, ...]"
    defaults_carriers: Mapping[str, str]
    _by_path: Mapping[str, Entry] = _field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_path", MappingProxyType({e.path: e for e in self.entries}))

    def entry(self, rel: str) -> Optional[Entry]:
        """The entry at root-relative *rel* ('/'), or None when not listed."""
        return self._by_path.get(rel)

    def config_files(self) -> "list[Entry]":
        """Every config-file entry (role `config`), in walk order.

        ⚠️ Includes the config files no reader reads (`readers == ()`, e.g.
        an unselected `_defaults.yml`) and those whose read fails (a
        `problem`): a consumer that wants "what is read" looks at `readers`
        itself."""
        return [e for e in self.entries if e.config_file]


def _check_list(mode: str) -> "Callable[[dict], None]":
    def check(doc: dict) -> None:
        for field in ("files", "rendered_by"):
            if field in doc:
                raise ValueError(f"{mode} mode with {field!r}")
        _check_index(doc, frozenset({mode}))
    return check


def run_list(root: Path | str, binary: Optional[str] = None,
             timeout: Optional[float] = None) -> dict:
    """`da-guard parse-tree --list-only --config-dir <root>`'s document,
    checked whole. Raises `BinaryMissing` when there is no binary and
    `ParseTreeError` when it fails, is older than `--list-only`, or writes
    another document — a file name that is not valid UTF-8 anywhere in the
    tree included (JSON cannot carry it, so da-guard refuses the listing)."""
    return _run([SUBCOMMAND, "--list-only", "--config-dir", str(root)], binary, timeout,
                check=_check_list("list"), what=f"{SUBCOMMAND} --list-only")


def list_tree(root: Path | str, binary: Optional[str] = None,
              timeout: Optional[float] = None) -> Listing:
    """`run_list`'s document as a `Listing` — a fresh walk on every call; a
    reader that needs one snapshot for several questions keeps the
    `Listing` and passes it down. Raises as `run_list`."""
    doc = run_list(root, binary, timeout)
    return Listing(Path(root), entries_of(doc), MappingProxyType(dict(doc["defaults_carriers"])))


def listing_of(tree: Tree, root: Path | str) -> Listing:
    """The `Listing` a tree-mode document already holds (its `entries` and
    `defaults_carriers`), *root* being the directory it was run on. ⛔ For a
    reader that holds the document (#3206): its file list is the snapshot
    it reads the content from, never a second walk that may see another
    tree. The roles are the same in both modes (pkg/config's verdict); a
    tree-mode entry's `problem` may also be a content kind (a syntax error)."""
    return Listing(Path(root), tree.entries,
                   MappingProxyType(dict(tree.defaults_carriers or {})))


#: One names-mode answer per (name, da-guard): a name's class is a pure
#: function of the name and the binary that answers, so this memo cannot go
#: stale while a tree changes (listings are never memoised). The da-guard
#: part of the key is `_names_key`.
_NAMES: "dict[tuple[str, tuple[Optional[str], str]], Entry]" = {}


def _names_key(binary: Optional[str]) -> "tuple[Optional[str], str]":
    """The binary a names run would execute — resolved now, to an absolute
    path, exactly as `_run` resolves it (None: none found) — and the current
    `$DA_GUARD_BINARY`: a memo keyed on what the caller passed (often None)
    would answer from one binary after `$DA_GUARD_BINARY` or the selection
    moved to another."""
    binary = _resolve(binary, None)[0]
    exe = guard_dispatch.DISPATCHER.resolve_binary(binary)
    return (os.path.abspath(exe) if exe is not None else None,
            os.environ.get("DA_GUARD_BINARY", ""))


def classify_names(names: "Iterable[str]", binary: Optional[str] = None,
                   timeout: Optional[float] = None) -> "dict[str, Entry]":
    """Each of *names* (base names, not on a disk: e.g. a ConfigMap's keys)
    → what the name alone makes it, memoised per name and da-guard for the
    process: one `parse-tree --list-only --names -` run for every name not
    yet asked. Raises as `run_list`; a name that is empty, holds a separator
    or is not valid UTF-8 refuses all."""
    names = list(dict.fromkeys(names))
    key = _names_key(binary)
    missing = [n for n in names if (n, key) not in _NAMES]
    if missing:
        for n, e in _run_names(missing, binary, timeout).items():
            _NAMES[(n, key)] = e
    return {n: _NAMES[(n, key)] for n in names}


def _run_names(names: "list[str]", binary: Optional[str],
               timeout: Optional[float]) -> "dict[str, Entry]":
    """One names-mode run over *names* (distinct), checked whole."""
    for n in names:
        try:
            n.encode("utf-8")
        except UnicodeEncodeError:
            # `os.fsdecode`'s lone surrogates: JSON cannot carry the name,
            # and encoding it anyway hands da-guard a different one.
            raise ParseTreeError(f"da-guard {SUBCOMMAND} --list-only --names: the name "
                                 f"{n!r} is not valid UTF-8; JSON cannot carry it") from None
    raw = json.dumps(names, ensure_ascii=False).encode("utf-8")
    doc = _run([SUBCOMMAND, "--list-only", "--names", "-"], binary, timeout, stdin=raw,
               check=_check_list("names"), what=f"{SUBCOMMAND} --list-only --names")
    entries = entries_of(doc)
    if [e.path for e in entries] != names:
        raise ParseTreeError(f"da-guard {SUBCOMMAND} --list-only --names did not answer the "
                             "names it was given, in order; nothing of it is used")
    return {e.path: e for e in entries}
