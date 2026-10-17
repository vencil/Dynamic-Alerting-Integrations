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

Used by `ops/migration_audit` and by `ops/config_diff`'s platform-settings
comparison (ADR-037 decision 3).

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
import re
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


class ParseTreeError(Exception):
    """parse-tree gave no document this module can read; the message says why."""


class BinaryMissing(ParseTreeError):
    """No da-guard binary at the explicit path, `$DA_GUARD_BINARY` or `$PATH`."""


class FileUnreadable(ParseTreeError):
    """A selected file parse-tree could not read: `kind` is its closed-list code
    (internal/parsetree Kinds), `message` the reason for a person."""

    def __init__(self, file: str, kind: str, message: str) -> None:
        super().__init__(f"{file}: {kind}: {message}")
        self.file = file
        self.kind = kind
        self.message = message


def run(root: Path | str, binary: Optional[str], timeout: float) -> dict:
    """`da-guard parse-tree --config-dir <root>`'s document. Raises
    `BinaryMissing` when there is no binary, `ParseTreeError` when it fails or
    its output is not a parse-tree document this reader reads."""
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is None:
        msg = dispatcher.binary_missing_message(binary).strip()
        for prefix in ("Error: ", "錯誤: "):
            msg = msg.removeprefix(prefix)
        raise BinaryMissing(msg)
    cmd = [exe, SUBCOMMAND, "--config-dir", str(root)]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ParseTreeError(f"da-guard {SUBCOMMAND} did not finish within {timeout}s") from e
    except OSError as e:
        raise ParseTreeError(f"da-guard could not be run ({exe}): {e}") from e
    stderr = proc.stderr.decode("utf-8", errors="backslashreplace").strip()
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

@dataclass(frozen=True)
class Timestamp:
    """A `!!timestamp` scalar: only its source text. Not a `str` — yaml.v3 and
    the exporter do not read it as one — and not a `datetime`: what instant it
    names is no reader's question here."""
    text: str


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


def to_python(node: Any) -> Any:
    """A parse-tree node as Python values: a mapping is a `dict` keyed by the
    JSON keys as written (each key's source text), a sequence a `list`, a
    scalar by its tag (see the module docstring). Any other tag or shape
    raises ValueError — `check_shape` refuses those first; this is the second
    line. Iterative: a document deep enough to decode is deep enough to
    convert."""
    out, fill = _new(node)
    stack = [(node, out)] if fill else []
    while stack:
        src, dst = stack.pop()
        items = src.items() if isinstance(src, dict) else enumerate(src)
        for k, child in items:
            v, more = _new(child)
            if isinstance(dst, dict):
                dst[k] = v
            else:
                dst.append(v)
            if more:
                stack.append((child, v))
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
    reader that needs it must refuse such a document."""
    selected: Mapping[str, tuple[str, ...]]
    unreadable: Mapping[str, tuple[str, str]]
    not_read: Mapping[str, str]
    rendered_by: Optional[Mapping[str, Mapping[str, tuple[str, ...]]]]
    _files: Mapping[str, Any]

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
    return Tree(
        selected=MappingProxyType({f: tuple(rs) for f, rs in doc["selected"].items()}),
        unreadable=MappingProxyType(unreadable),
        not_read=MappingProxyType(not_read),
        rendered_by=rendered_by,
        _files=MappingProxyType(dict(doc["files"])),
    )


def load(root: Path | str, binary: Optional[str], timeout: float) -> Tree:
    """`run`'s document as a `Tree`. Raises as `run` does, and as `tree_of`."""
    return tree_of(run(root, binary, timeout))
