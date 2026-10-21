"""_lib_parse_tree.py — read `da-guard parse-tree`'s document (ADR-036 §2).

`da-guard parse-tree --config-dir <dir>` writes, as one JSON document, the
files of a conf.d tree as yaml.v3 parses them: which files each reader
selects, each readable file's content as a tree of tagged nodes, the files it
could not read and why. ADR-036 makes Go the only YAML parser of conf.d; a
Python tool that needs the content of a file reads it from this document
instead of parsing the YAML itself.

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

⛔ KEY ORDER IS NOT THE FILE'S. parse-tree writes each mapping from a Go map,
and encoding/json writes a map's keys sorted (byte order). Measured on a file
written `zeta, alpha{mike, bravo, "10", "9"}, Mid, _b`: the document holds
`Mid, _b, alpha{10, 9, bravo, mike}, zeta`. The order of a `Tree.value`
mapping is that sorted order; a reader that needs the order the author wrote
cannot get it from this document.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional

_HERE = Path(__file__).resolve().parent
# guard_dispatch lives in ops/ in the repo; the image ships it flat beside
# this module. Appended, so nothing there shadows a module already found.
if (_HERE / "ops").is_dir() and str(_HERE / "ops") not in sys.path:
    sys.path.append(str(_HERE / "ops"))

import guard_dispatch  # noqa: E402

SUBCOMMAND = "parse-tree"
SCHEMA = "da-guard.parse-tree/v1"

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


def run(root: Path | str, binary: Optional[str], timeout: float) -> dict:
    """`da-guard parse-tree --config-dir <root>`'s document. Raises
    `BinaryMissing` when there is no binary, `ParseTreeError` when it fails or
    its output is not a parse-tree document this reader reads."""
    return _run([SUBCOMMAND, "--config-dir", str(root)], binary, timeout)


def run_file(root: Path | str, rel: str, text: bytes,
             binary: Optional[str], timeout: float) -> dict:
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


def _run(args: list[str], binary: Optional[str], timeout: float,
         stdin: Optional[bytes] = None) -> dict:
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
        raise ParseTreeError(f"da-guard {SUBCOMMAND} did not finish within {timeout}s") from e
    except OSError as e:
        raise ParseTreeError(f"da-guard could not be run ({exe}): {e}") from e
    stderr = proc.stderr.decode("utf-8", errors="backslashreplace").strip()
    if _MEMORY_LIMITED and proc.returncode < 0:
        # Ended by a signal under the memory limit it inherited (the
        # generator container's RLIMIT_DATA of a judgement, ADR-036 §3):
        # the caller's memory limit, not a da-guard defect.
        raise MemoryError(f"da-guard {SUBCOMMAND} was ended by "
                          f"{_signal_name(-proc.returncode)} under this process's "
                          f"memory limit (RLIMIT_DATA)")
    if proc.returncode != 0:
        older = ""
        if proc.returncode == 2 and not _accepts(cmd, timeout):
            older = (f" — this da-guard does not know `{SUBCOMMAND}`: it is older "
                     "than this tool; upgrade or rebuild it")
        raise ParseTreeError(f"da-guard {SUBCOMMAND} exited {proc.returncode}{older}"
                             + (f": {' '.join(stderr.split())}" if stderr else ""))
    try:
        doc = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            got = doc.get("schema") if isinstance(doc, dict) else None
            raise ValueError(f"schema {got!r}, this reader reads {SCHEMA!r}")
        check_shape(doc)
    except (ValueError, TypeError, KeyError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        # ADR-036 §2: a document a reader cannot decode is unusable whole.
        raise ParseTreeError(f"da-guard {SUBCOMMAND} wrote no document this tool can "
                             f"read ({e}); nothing of it is used") from e
    return doc


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def check_shape(doc: dict) -> None:
    """Raise ValueError unless `doc` has the parse-tree shape this module reads,
    every element of it: a document that is wrong anywhere is unusable whole."""
    for field, kind in (("selected", dict), ("files", dict), ("unreadable", list),
                        ("not_read", list)):
        if not isinstance(doc.get(field), kind):
            raise ValueError(f"field {field!r} missing or not a {kind.__name__}")
    for f, readers in doc["selected"].items():
        if not (isinstance(readers, list) and all(isinstance(r, str) for r in readers)):
            raise ValueError(f"selected[{f!r}] is not a list of reader names")
    for name, fields in (("unreadable", ("file", "kind", "message")), ("not_read", ("file", "reason"))):
        for i, e in enumerate(doc[name]):
            if not (isinstance(e, dict) and all(isinstance(e.get(k), str) for k in fields)):
                raise ValueError(f"{name}[{i}] is not an object with string {', '.join(fields)}")
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
    # parse-tree's own invariant: "selected" is "files" plus "unreadable".
    unreadable = {u["file"] for u in doc["unreadable"]}
    if set(doc["selected"]) != set(doc["files"]) | unreadable or unreadable & set(doc["files"]):
        raise ValueError("\"selected\" is not \"files\" plus \"unreadable\"")


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
    """One parse-tree document, read-only. `selected`: every file some reader
    reads → its readers; `unreadable`: file → (kind, message); `not_read`:
    file → reason; `rendered_by`: the document's `rendered_by` (file → key →
    readers), or None when the document has none — the field was added
    beside the v1 fields (parse_tree.go), so a v1 document may lack it, and a
    reader that needs it must refuse such a document. `defaults_carriers`:
    directory ("." for the root) → the one defaults carrier its chain reads,
    or None when the document has none (`_lib_parse_cache.defaults_carriers`
    refuses such a document)."""
    selected: Mapping[str, tuple[str, ...]]
    unreadable: Mapping[str, tuple[str, str]]
    not_read: Mapping[str, str]
    rendered_by: Optional[Mapping[str, Mapping[str, tuple[str, ...]]]]
    _files: Mapping[str, Any]
    defaults_carriers: Optional[Mapping[str, str]] = None

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
    """A `Tree` of a document `check_shape` accepts (`run` checks it). Raises
    ParseTreeError when a file is listed twice or `rendered_by` is malformed:
    a document wrong anywhere is unusable whole."""
    def once(name: str, entries: list, field: str) -> dict:
        out: dict = {}
        for e in entries:
            if e["file"] in out:
                raise ParseTreeError(f"da-guard {SUBCOMMAND}: {e['file']!r} is in {name!r} twice; "
                                     "nothing of the document is used")
            out[e["file"]] = e[field] if field else (e["kind"], e["message"])
        return out

    unreadable = once("unreadable", doc["unreadable"], "")
    not_read = once("not_read", doc["not_read"], "reason")
    rendered_by = None
    if "rendered_by" in doc:
        rb = doc["rendered_by"]
        ok = isinstance(rb, dict) and all(
            isinstance(keys, dict) and all(
                isinstance(rs, list) and all(isinstance(r, str) for r in rs) for rs in keys.values())
            for keys in rb.values())
        if not ok:
            raise ParseTreeError(f"da-guard {SUBCOMMAND}: \"rendered_by\" is not "
                                 "{file: {key: [reader]}}; nothing of the document is used")
        rendered_by = MappingProxyType({f: MappingProxyType({k: tuple(rs) for k, rs in keys.items()})
                                        for f, keys in rb.items()})
    carriers = None
    if "defaults_carriers" in doc:
        dc = doc["defaults_carriers"]
        if not (isinstance(dc, dict) and all(isinstance(v, str) for v in dc.values())):
            raise ParseTreeError(f"da-guard {SUBCOMMAND}: \"defaults_carriers\" is not "
                                 "{dir: path}; nothing of the document is used")
        carriers = MappingProxyType(dict(dc))
    return Tree(
        selected=MappingProxyType({f: tuple(rs) for f, rs in doc["selected"].items()}),
        unreadable=MappingProxyType(unreadable),
        not_read=MappingProxyType(not_read),
        rendered_by=rendered_by,
        _files=MappingProxyType(dict(doc["files"])),
        defaults_carriers=carriers,
    )


def load(root: Path | str, binary: Optional[str], timeout: float) -> Tree:
    """`run`'s document as a `Tree`. Raises as `run` does, and as `tree_of`."""
    return tree_of(run(root, binary, timeout))


def load_file(root: Path | str, rel: str, text: bytes,
              binary: Optional[str], timeout: float) -> Tree:
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
