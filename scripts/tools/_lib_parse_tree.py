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

Used by `ops/migration_audit`; `ops/config_diff`'s carrier table (#1516 P3,
ADR-037 decision 3) is its next reader.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

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
