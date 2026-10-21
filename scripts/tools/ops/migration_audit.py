#!/usr/bin/env python3
"""migration_audit.py — `da-tools migration-audit`: list where PyYAML and parse-tree differ.

One conf.d read with both YAML readers, differences listed (ADR-036 §8, #2898).

ADR-036 moves every Python reader of conf.d onto `da-guard parse-tree` (Go,
yaml.v3). Before that switch this command shows, on the customer's own tree,
what the switch changes. It reads the tree twice:

* **today** — the PyYAML reading the route generator used before ADR-036
  step 4 (#2906), kept here as this module's own: the files the generator
  reads (the shared conf.d walk, one defaults carrier per directory), each
  loaded with `_lib_io.strict_load_exporter_keys`,
  `raw_text_sequences=("tenants",)` — the PyYAML loader every conf.d reader
  that is not yet on parse-tree still uses. The generator itself reads
  parse-tree's document since step 4, so it is not called here;
* **after** — `da-guard parse-tree --config-dir <dir>` (ADR-036 §2), run as a
  subprocess. Its JSON is the only yaml.v3 reading here: this module never
  parses YAML the Go way itself, and without a da-guard binary it stops
  (exit 2) instead of falling back to a reading of its own (ADR-036, "失敗時
  會怎樣", last row).

and lists, without judging any of it:

1. ``values``   — a value the two readers read differently: file, line, both
   readings, and a suggested spelling both read the same way;
2. ``keys``     — a key one reader has and the other does not;
3. ``becomes_unreadable`` — a file today's reader reads whole that
   parse-tree refuses, with parse-tree's ``kind``;
4. ``becomes_readable``   — a file today's reader refuses whole that
   parse-tree reads (§2's closed list), with what yaml.v3 reads in it;

plus ``selection`` (a file one side reads and the other does not).
``still_unreadable`` lists the files both readers refuse; that is no change,
so it does not set the exit code.

Lines are 1-based and come from PyYAML's reading of the file: a value is
reported at the line it is written on, a key at its own line. A value reached
through an alias or a merge is reported at the line of its anchor or of the
merged mapping's definition — changing that line changes every place that
uses it. A key only yaml.v3 has carries the line of its parent.

Exit codes (dev-rules §13, `_lib_exitcodes`):
  0  the two readings agree (``still_unreadable`` may be non-empty)
  1  a difference is listed
  2  caller error: --config-dir missing or unreadable, no da-guard binary,
     da-guard failed or wrote no parse-tree document it can be read from

`--json`: stdout is exactly one JSON document on every path (schema
``da-tools.migration-audit/v1``; ``files_compared`` names the files both
readers read and that were compared value by value, so "no difference"
and "nothing compared" read apart); on exit 2 it carries ``status: "caller_error"``
and ``reason`` with every list empty.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Optional

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.dirname(_HERE)
for _p in (_TOOLS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _lib_confd import (  # noqa: E402
    is_defaults_name,
    list_config_tree,
    readable_carriers,
    select_defaults_carrier,
    unusable_reason,
)
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, arm_crash_envelope, disarm_crash_envelope,
)
from _lib_io import safe_label, strict_load_exporter_keys  # noqa: E402
from _lib_python import detect_cli_lang, positive_seconds  # noqa: E402
import _lib_parse_tree  # noqa: E402

SCHEMA = "da-tools.migration-audit/v1"
PARSE_TREE_SUBCOMMAND = _lib_parse_tree.SUBCOMMAND
PARSE_TREE_SCHEMA = _lib_parse_tree.SCHEMA
DEFAULT_TIMEOUT = 600

# The lists whose entries are differences (exit 1); `still_unreadable` is not.
DIFFERENCE_LISTS = ("selection", "values", "keys", "becomes_unreadable",
                    "becomes_readable")
ALL_LISTS = DIFFERENCE_LISTS + ("still_unreadable",)

_NULL_TAG = "tag:yaml.org,2002:null"
# A cap on the nodes one file's comparison visits (aliases can make a small
# file a large tree). Reaching it is listed, never a silent partial pass.
MAX_NODES = 1_000_000


class AuditError(Exception):
    """The audit cannot run (exit 2); the message says why."""


# ── today: the route generator's reading ─────────────────────────────────

def _read_today(path: Path) -> tuple[Optional[Any], Optional[str]]:
    """(data, None) or (None, reason): one file as the PyYAML reading reads
    it — the read loop the route generator had before ADR-036 step 4: same
    open, same loader, same whole-file drops. `tests/ops/test_migration_audit.py`
    holds this to the generator's frozen verdict on every row of
    tests/shared/parse_tree_corpus.json (frozen because the generator no
    longer reads this way)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = strict_load_exporter_keys(f, raw_text_sequences=("tenants",))
    except yaml.YAMLError as e:
        return None, f"failed to parse: {' '.join(str(e).split())}"
    except UnicodeDecodeError as e:
        return None, f"not valid UTF-8 ({e.reason} at byte {e.start})"
    except Exception as e:  # noqa: BLE001 — as the generator: any failure names the file
        return None, f"could not be read — {e.__class__.__name__}: {' '.join(str(e).split())}"
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, f"top level must be a mapping, got {type(data).__name__}"
    return data, None


def read_tree_today(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """({rel: data} read, {rel: reason} refused) — the files the route
    generator reads, by its selection (`_grar_parse._parse_config_files`):
    the shared walk (`list_config_tree`), one defaults carrier per directory
    (`select_defaults_carrier` over the readable ones), every unusable entry
    booked as refused."""
    listing = list_config_tree(root)
    if any(p == root for p in listing.unusable):
        raise AuditError(f"config directory could not be read: {root} — {unusable_reason(root)}")
    read: dict[str, Any] = {}
    refused: dict[str, str] = {}
    for bad in listing.unusable:
        refused[bad.relative_to(root).as_posix()] = unusable_reason(bad)
    carriers: dict[Path, list[Path]] = {}
    for p in listing.files:
        if is_defaults_name(p.name):
            carriers.setdefault(p.parent, []).append(p)
    selected: dict[Path, Optional[Path]] = {}
    readable_set: set[Path] = set()
    for d, cs in carriers.items():
        readable, _ = readable_carriers(cs)
        readable_set.update(readable)
        selected[d] = select_defaults_carrier(readable)
    for p in listing.files:
        rel = p.relative_to(root).as_posix()
        chosen = selected.get(p.parent)
        if (is_defaults_name(p.name) and p in readable_set
                and (chosen is None or p.name != chosen.name)):
            continue  # the unselected spelling: not read
        data, reason = _read_today(p)
        if reason is None:
            read[rel] = data
        else:
            # The loader names the file by its full path; the report by rel.
            refused[rel] = reason.replace(str(p), rel)
    return read, refused


# ── after: da-guard parse-tree ───────────────────────────────────────────

def run_parse_tree(root: Path, binary: Optional[str], timeout: float) -> dict:
    """`da-guard parse-tree --config-dir <root>`'s document (`_lib_parse_tree`).
    Raises AuditError when there is no binary, it fails, or its output is not a
    parse-tree document this reader reads — never a partial reading."""
    try:
        return _lib_parse_tree.run(root, binary, timeout)
    except _lib_parse_tree.BinaryMissing as e:
        raise AuditError(
            str(e) + "\nThis audit reads yaml.v3's reading from `da-guard parse-tree` only; it does "
            "not parse the YAML another way (the da-tools image ships da-guard; in a checkout, "
            "`make da-guard-build` builds .build/da-guard).") from e
    except _lib_parse_tree.ParseTreeError as e:
        raise AuditError(str(e)) from e


# ── comparing one file ───────────────────────────────────────────────────

_is_v3_scalar = _lib_parse_tree.is_scalar


def _py_kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, (_dt.date, _dt.datetime)):
        return "timestamp"
    if isinstance(v, dict):
        return "mapping"
    if isinstance(v, list):
        return "sequence"
    return type(v).__name__


def _py_json(v: Any) -> dict:
    """Today's reading of one value, as JSON."""
    kind = _py_kind(v)
    out: dict[str, Any] = {"type": kind}
    if kind in ("null", "bool", "int", "str"):
        out["value"] = v
    elif kind == "float":
        out["value"] = repr(v) if (math.isinf(v) or math.isnan(v)) else v
    elif kind == "timestamp":
        out["value"] = v.isoformat()
    elif kind in ("mapping", "sequence"):
        out["size"] = len(v)
    else:
        out["value"] = repr(v)
    return out


def _v3_json(v: Any) -> dict:
    """yaml.v3's reading of one value, as JSON (a scalar as parse-tree has it)."""
    if _is_v3_scalar(v):
        return dict(v)
    if isinstance(v, dict):
        return {"type": "mapping", "size": len(v)}
    if isinstance(v, list):
        return {"type": "sequence", "size": len(v)}
    return {"type": type(v).__name__}


def _special_float(text: str) -> float:
    t = text.strip().lower().lstrip("+")
    if t.endswith("nan"):
        return math.nan
    return -math.inf if t.startswith("-") else math.inf


def same_scalar(py: Any, v3: dict) -> bool:
    """Whether today's value `py` is what yaml.v3 reads (`v3`: text, tag,
    value) — the same type and the same value."""
    tag, value = v3["tag"], v3.get("value")
    kind = _py_kind(py)
    if tag == "!!str":
        return kind == "str" and py == value
    if tag == "!!bool":
        return kind == "bool" and py is value
    if tag == "!!null":
        return kind == "null"
    if tag == "!!int":
        return kind == "int" and str(py) == value
    if tag == "!!float":
        if kind != "float":
            return False
        if value is None:
            want = _special_float(v3["text"])
            return math.isnan(py) if math.isnan(want) else py == want
        return py == float(value)
    if tag == "!!timestamp":
        return kind == "timestamp"
    return False


# Integers both readers read as the same integer: yaml.v3 decodes into int64,
# or uint64 above it; anything else it reads as a (lossy) float.
_INT_MIN, _INT_END = -(2 ** 63), 2 ** 64


def _float_text(f: float) -> str:
    """A spelling both readers read as the float `f`: `.inf` / `-.inf` /
    `.nan`, else Python's shortest repr with a `.` in the mantissa (PyYAML's
    YAML 1.1 float needs one: `1e+20` is a string to it, `1.0e+20` a float)."""
    if math.isnan(f):
        return ".nan"
    if math.isinf(f):
        return ".inf" if f > 0 else "-.inf"
    r = repr(f)
    mantissa, _, exp = r.partition("e")
    if "." not in mantissa:
        mantissa += ".0"
    return mantissa + ("e" + exp if exp else "")


def rewrites(py: Any, v3: dict, raw_text: bool) -> list[dict]:
    """Spellings both readers read the same way, for a scalar they differ on:
    [{"meaning": "string" | "boolean" | "number" | "null", "text": <YAML>}],
    one per meaning either reader gave it (and always the quoted string).
    Each is the plain YAML to write in place of the value; none is lossy —
    an integer neither reads exactly, a time, a non-finite float as a number
    other than `.inf`/`.nan` get no number spelling. `raw_text`: today's
    reader took this value's source text whatever its type (a `tenants:`
    list), so only a quoted string reads the same in both.
    tests/ops/test_migration_audit.py writes every one back and re-audits."""
    text, tag, kind = v3.get("text", ""), v3.get("tag"), _py_kind(py)
    out: list[dict] = []

    def add(meaning: str, t: str) -> None:
        if t != text and all(o["text"] != t for o in out):
            out.append({"meaning": meaning, "text": t})

    def number(meaning_value: Any) -> None:
        if isinstance(meaning_value, int) and not isinstance(meaning_value, bool):
            if _INT_MIN <= meaning_value < _INT_END:
                add("number", str(meaning_value))
        elif isinstance(meaning_value, float):
            add("number", _float_text(meaning_value))

    # An integer past what yaml.v3 holds exactly (it reads a float): any
    # number written there loses digits in one reader, so only the string.
    lossy_int = kind == "int" and not (_INT_MIN <= py < _INT_END)
    if not raw_text and not lossy_int:
        # What today's reader meant.
        if kind == "bool":
            add("boolean", "true" if py else "false")
        elif kind in ("int", "float"):
            number(py)
        elif kind == "null":
            add("null", "null")
        # What yaml.v3 means.
        value = v3.get("value")
        if tag == "!!bool":
            add("boolean", "true" if value else "false")
        elif tag == "!!null":
            add("null", "null")
        elif tag == "!!int" and isinstance(value, str):
            number(int(value))
        elif tag == "!!float":
            number(float(value) if isinstance(value, str) else _special_float(text))
    strings = [py] if kind == "str" else []
    if tag == "!!str" and isinstance(v3.get("value"), str):
        strings.append(v3["value"])
    for st in strings + [text]:
        add("string", json.dumps(st, ensure_ascii=False))
    return out


# Fields whose values the route generator reads from a closed word list
# (#2906): a rewrite it refuses is no rewrite. `_severity_dedup` takes a
# boolean, null or one of SEVERITY_DEDUP_WORDS (any case) — `"no"` is refused.
def _field_options(path: list, options: list[dict]) -> list[dict]:
    if not (path and path[-1] == "_severity_dedup"):
        return options
    from _grar_parse import SEVERITY_DEDUP_WORDS  # the generator's word list, not its reading
    out: list[dict] = []
    for o in options:
        if o["meaning"] == "string":
            try:
                word = json.loads(o["text"])
            except ValueError:
                continue
            if not (isinstance(word, str) and word.strip().lower() in SEVERITY_DEDUP_WORDS):
                continue
        out.append(o)
        if o["meaning"] == "boolean":
            word = '"enable"' if o["text"] == "true" else '"disable"'
            if all(x["text"] != word for x in options):
                out.append({"meaning": "string", "text": word})
    return out


def suggestion(options: list[dict], written: Optional[str] = None) -> str:
    """`written`: the value as written (its tag included, its anchor not)."""
    if not options:
        return "the two readers build a different structure here; rewrite it so both read the same"
    head = f"replace {written} — " if written else ""
    return head + "; ".join(f"if a {o['meaning']} is meant, write {o['text']}" for o in options)


class _Walk:
    """One file's comparison: today's data, PyYAML's composed nodes (for
    line numbers only) and parse-tree's content, walked together."""

    def __init__(self, rel: str, text: str) -> None:
        self.rel = rel
        self.values: list[dict] = []
        self.keys: list[dict] = []
        self.capped = False
        self._nodes = 0
        self._loader: Any = None
        self._lines = text.split("\n")
        self.root_node: Any = None
        try:
            # Composition only (no constructor runs): PyYAML's own nodes, for
            # the line of each entry. SafeLoader composes exactly as the
            # generator's loader (a SafeLoader subclass) does.
            self._loader = yaml.SafeLoader(text)
            self.root_node = self._loader.get_single_node()
        except Exception:  # noqa: BLE001 — lines are a help, the comparison does not need them
            self.root_node = None

    def close(self) -> None:
        if self._loader is not None:
            self._loader.dispose()

    def _pairs(self, node: Any) -> dict[str, tuple[Any, Any]]:
        """{key text: (key node, value node)} of a mapping node, merges
        flattened as PyYAML's constructor flattens them, last one wins."""
        if not isinstance(node, yaml.MappingNode):
            return {}
        try:
            self._loader.flatten_mapping(node)
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, tuple[Any, Any]] = {}
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.tag != _NULL_TAG:
                out[k.value] = (k, v)
        return out

    @staticmethod
    def _line(node: Any) -> Optional[int]:
        return node.start_mark.line + 1 if node is not None else None

    def run(self, py: dict, v3: dict) -> None:
        # (path, today, yaml.v3, node of the value, line of the entry)
        stack: list[tuple[list, Any, Any, Any, Optional[int]]] = [([], py, v3, self.root_node, None)]
        while stack:
            path, a, b, node, line = stack.pop()
            self._nodes += 1
            if self._nodes > MAX_NODES:
                self.capped = True
                return
            if _is_v3_scalar(b):
                if not same_scalar(a, b):
                    # Today's reader keeps the source text of every item of
                    # a `tenants:` list (raw_text_sequences), whatever it is.
                    raw = len(path) >= 2 and isinstance(path[-1], int) and path[-2] == "tenants"
                    self._value(path, line, a, b,
                                _field_options(path, rewrites(a, b, raw)), node)
                continue
            if isinstance(b, dict) and isinstance(a, dict):
                pairs = self._pairs(node)
                for k in sorted(set(a) | set(b), reverse=True):
                    kn, vn = pairs.get(k, (None, None))
                    kline = self._line(kn) if kn is not None else line
                    if k not in b:
                        self.keys.append({"file": self.rel, "line": kline, "path": path, "key": k,
                                          "only_in": "pyyaml", "pyyaml": _py_json(a[k])})
                    elif k not in a:
                        self.keys.append({"file": self.rel, "line": kline, "path": path, "key": k,
                                          "only_in": "yaml_v3", "yaml_v3": _v3_json(b[k])})
                    else:
                        # The line the value is written on: an aliased or
                        # merged value's is its anchor's / merged mapping's.
                        stack.append((path + [k], a[k], b[k], vn,
                                      self._line(vn) if isinstance(vn, yaml.ScalarNode) else kline))
                continue
            if isinstance(b, list) and isinstance(a, list) and len(a) == len(b):
                items = node.value if isinstance(node, yaml.SequenceNode) else [None] * len(b)
                for i in range(len(b) - 1, -1, -1):
                    item = items[i] if i < len(items) else None
                    stack.append((path + [i], a[i], b[i], item,
                                  self._line(item) if item is not None else line))
                continue
            self._value(path, line, a, b, [])

    def _span(self, node: Any) -> Optional[dict]:
        """Where a one-line value is written: 1-based line, 0-based column
        range of its tag and text (an anchor before them is not part of it)."""
        if not isinstance(node, yaml.Node):
            return None
        s, e = node.start_mark, node.end_mark
        if s.line != e.line or s.line >= len(self._lines):
            return None
        src = self._lines[s.line]
        col = s.column
        if src[col:col + 1] == "&":  # `&name value`: the anchor stays
            sp = src.find(" ", col)
            if sp < 0 or sp >= e.column:
                return None
            col = sp + 1
            while col < e.column and src[col] == " ":
                col += 1
        return {"line": s.line + 1, "start": col, "end": e.column, "written": src[col:e.column]}

    def _value(self, path: list, line: Optional[int], a: Any, b: Any, options: list,
               node: Any = None) -> None:
        span = self._span(node) if options else None
        entry = {"file": self.rel, "line": line, "path": path,
                 "pyyaml": _py_json(a), "yaml_v3": _v3_json(b), "rewrites": options,
                 "suggestion": suggestion(options, span["written"] if span else None)}
        if span:
            entry["span"] = span
        self.values.append(entry)


# ── the audit ────────────────────────────────────────────────────────────

def empty_report() -> dict:
    return {"schema": SCHEMA, "status": "ok", "config_dir": None, "da_guard_version": None,
            "pyyaml_version": yaml.__version__,
            "summary": {**{k: 0 for k in ALL_LISTS}, "files_compared": 0},
            "files_compared": [], **{k: [] for k in ALL_LISTS}}


def audit(root: Path, binary: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """The report for the conf.d at `root`. Raises AuditError (exit 2)."""
    if not root.is_dir():
        raise AuditError(f"config directory not found: {root}")
    doc = run_parse_tree(root, binary, timeout)
    today_read, today_refused = read_tree_today(root)
    report = empty_report()
    report["config_dir"] = str(root)
    report["da_guard_version"] = doc.get("da_guard_version")

    entries = {e.path: e for e in _lib_parse_tree.entries_of(doc)}
    # The reader's own `selected` (content or a problem): no filter of ours. A
    # config-named directory is in it by its problem, as today's reader
    # refuses it too; a plain directory (readers []) is not.
    after_selected = set(_lib_parse_tree.tree_of(doc).selected)
    after_unreadable = {f: {"kind": e.problem[0], "message": e.problem[1]}
                        for f, e in entries.items() if e.problem is not None}
    today_selected = set(today_read) | set(today_refused)

    for f in sorted(after_selected - today_selected):
        report["selection"].append({"file": f, "read_by": "yaml_v3",
                                    "readers": list(entries[f].readers),
                                    "detail": "parse-tree reads it; today's reader does not"})
    for f in sorted(today_selected - after_selected):
        report["selection"].append({"file": f, "read_by": "pyyaml",
                                    "detail": "today's reader reads it; parse-tree does not"})

    for f in sorted(after_selected & today_selected):
        bad = after_unreadable.get(f)
        if bad is not None:
            entry = {"file": f, "kind": bad["kind"], "message": bad["message"]}
            if f in today_refused:
                report["still_unreadable"].append({**entry, "pyyaml_error": today_refused[f]})
            else:
                report["becomes_unreadable"].append(entry)
            continue
        if f in today_refused:
            report["becomes_readable"].append({"file": f, "pyyaml_error": today_refused[f],
                                               "yaml_v3": doc["files"].get(f, {})})
            continue
        report["files_compared"].append(f)
        text = (root / f).read_bytes().decode("utf-8")
        walk = _Walk(f, text)
        try:
            walk.run(today_read[f], doc["files"].get(f, {}))
        finally:
            walk.close()
        report["values"].extend(walk.values)
        report["keys"].extend(walk.keys)
        if walk.capped:
            report["values"].append({"file": f, "line": None, "path": [], "pyyaml": None,
                                     "yaml_v3": None, "rewrites": [],
                                     "suggestion": f"not compared past {MAX_NODES} nodes; "
                                                   "check this file by hand"})

    report["summary"] = {k: len(report[k]) for k in ALL_LISTS}
    report["summary"]["files_compared"] = len(report["files_compared"])
    return report


def has_differences(report: dict) -> bool:
    return any(report[k] for k in DIFFERENCE_LISTS)


# ── text output ──────────────────────────────────────────────────────────

def _path_text(path: list) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (("." if out else "") + str(p))
    return out or "(top level)"


def _reading(v: Optional[dict]) -> str:
    if not v:
        return "-"
    if "tag" in v:
        if "value" not in v:
            val = "(no value)"
        elif v["tag"] in ("!!int", "!!float"):
            val = str(v["value"])  # parse-tree's decimal text, shown as the number
        else:
            val = json.dumps(v["value"], ensure_ascii=False)
        return f"{v['tag']} {val} (written {json.dumps(v['text'], ensure_ascii=False)})"
    if "size" in v:
        return f"{v['type']} of {v['size']}"
    return f"{v['type']} {json.dumps(v.get('value'), ensure_ascii=False, default=str)}"


_MAX_INDENT = 40  # deeper levels print their depth instead of more indent


def _content_lines(v: Any) -> Iterator[str]:
    """yaml.v3's reading of one file, one line per entry. Iterative: a file
    yaml.v3 reads may nest thousands of levels (its limit is 10,000)."""
    if _is_v3_scalar(v):
        yield f"      {_reading(v)}"
        return
    stack: list[tuple[int, Any, Any, Any]] = []

    def push(depth: int, coll: Any) -> None:
        items = list(coll.items()) if isinstance(coll, dict) else list(enumerate(coll))
        for k, sub in reversed(items):
            stack.append((depth, coll, k, sub))

    push(0, v)
    while stack:
        depth, parent, k, sub = stack.pop()
        pad = "      " + "  " * min(depth, _MAX_INDENT)
        if depth > _MAX_INDENT:
            pad += f"(depth {depth}) "
        label = f"- [{k}]" if isinstance(parent, list) else f"{k}:"
        if _is_v3_scalar(sub):
            yield f"{pad}{label} {_reading(sub)}"
        else:
            yield f"{pad}{label}" + (" {}" if sub == {} else " []" if sub == [] else "")
            push(depth + 1, sub)


def _where(e: dict) -> str:
    return f"{e['file']}:{e['line']}" if e.get("line") else e["file"]


def render_text(report: dict) -> str:
    s = safe_label
    lines = [f"migration-audit: {s(str(report['config_dir']))} — PyYAML {report['pyyaml_version']} "
             f"(today) vs da-guard parse-tree {s(str(report['da_guard_version']))} (yaml.v3)",
             f"{len(report['files_compared'])} file(s) both read were compared value by value"
             + (": " + ", ".join(s(f) for f in report["files_compared"][:20])
                + (f", +{len(report['files_compared']) - 20} more"
                   if len(report["files_compared"]) > 20 else "")
                if report["files_compared"] else "")]
    sections = (
        ("selection", "Files only one side reads"),
        ("values", "Values read differently"),
        ("keys", "Keys only one side has"),
        ("becomes_unreadable", "Files read today that parse-tree refuses"),
        ("becomes_readable", "Files refused today that parse-tree reads"),
        ("still_unreadable", "Files both refuse (no change)"),
    )
    for key, title in sections:
        entries = report[key]
        if not entries:
            continue
        lines.append("")
        lines.append(f"{title} ({len(entries)}):")
        for e in entries:
            if key == "selection":
                lines.append(f"  {s(e['file'])}: {e['detail']}")
            elif key == "values":
                lines.append(f"  {s(_where(e))}  {s(_path_text(e['path']))}")
                lines.append(f"    today:   {s(_reading(e['pyyaml']))}")
                lines.append(f"    yaml.v3: {s(_reading(e['yaml_v3']))}")
                lines.append(f"    suggest: {s(e['suggestion'])}")
            elif key == "keys":
                side = "today's reader" if e["only_in"] == "pyyaml" else "yaml.v3"
                lines.append(f"  {s(_where(e))}  {s(_path_text(e['path']))}: key "
                             f"{s(json.dumps(e['key'], ensure_ascii=False))} only in {side}")
            elif key in ("becomes_unreadable", "still_unreadable"):
                lines.append(f"  {s(e['file'])}: {e['kind']} — {s(e['message'])}")
                if key == "still_unreadable":
                    lines.append(f"    today: {s(e['pyyaml_error'])}")
            else:
                lines.append(f"  {s(e['file'])}: today: {s(e['pyyaml_error'])}")
                lines.append("    yaml.v3 reads:" + (" {}" if not e["yaml_v3"] else ""))
                lines.extend(s(ln) for ln in _content_lines(e["yaml_v3"]))
    lines.append("")
    n = sum(report["summary"][k] for k in DIFFERENCE_LISTS)
    lines.append(f"{n} difference(s)" if n else "No differences: both readers read this tree the same way.")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────

# --help in the operator's language (dev-rules §9 L3); report strings stay English.
_HELP = {
    "en": {
        "description": ("Read a conf.d with PyYAML (the route generator's reader before ADR-036 step 4) and with "
                        "`da-guard parse-tree` (yaml.v3), and list where they differ "
                        "(ADR-036 §8). Lists differences; judges none."),
        "epilog": ("Exit codes: 0 no difference, 1 a difference is listed, 2 caller error "
                   "(no --config-dir, no da-guard, da-guard failed). "
                   f"--json: one document on stdout, schema {SCHEMA}."),
        "config_dir": "Path to the conf.d/ root.",
        "json": "Print the report as one JSON document.",
        "binary": "Path to da-guard (default: $DA_GUARD_BINARY, then da-guard on $PATH).",
        "timeout": f"Seconds da-guard may run (default {DEFAULT_TIMEOUT}).",
    },
    "zh": {
        "description": ("同一棵 conf.d 以 PyYAML（ADR-036 步驟 4 之前路由產生器的讀法）與 `da-guard parse-tree`"
                        "（yaml.v3）各讀一次，列出讀法差異（ADR-036 §8）。只列差異，不做判定。"),
        "epilog": ("結束碼：0 沒有差異、1 列出了差異、2 呼叫錯誤（--config-dir 不存在、找不到 "
                   f"da-guard、da-guard 失敗）。--json：stdout 恰好一份 JSON，schema {SCHEMA}。"),
        "config_dir": "conf.d/ 根目錄。",
        "json": "以一份 JSON 輸出報告。",
        "binary": "da-guard 路徑（預設依序：$DA_GUARD_BINARY、$PATH 上的 da-guard）。",
        "timeout": f"da-guard 執行上限秒數（預設 {DEFAULT_TIMEOUT}）。",
    },
}


def build_parser() -> argparse.ArgumentParser:
    h = _HELP["zh" if detect_cli_lang() == "zh" else "en"]
    p = argparse.ArgumentParser(prog="da-tools migration-audit", description=h["description"],
                                epilog=h["epilog"])
    p.add_argument("--config-dir", required=True, help=h["config_dir"])
    p.add_argument("--json", action="store_true", help=h["json"])
    p.add_argument("--da-guard-binary", default=None, help=h["binary"])
    p.add_argument("--timeout", type=positive_seconds, default=DEFAULT_TIMEOUT, help=h["timeout"])
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.json:
        # #3094: a crash in --json mode still prints one envelope, shaped
        # like the caller_error document below
        arm_crash_envelope(build=lambda: {**empty_report(), "config_dir": args.config_dir})
    try:
        report = audit(Path(args.config_dir), args.da_guard_binary, args.timeout)
        out = (json.dumps(report, ensure_ascii=False, default=str) if args.json
               else render_text(report))
    except Exception as e:  # noqa: BLE001 — every failure is exit 2 with a reason, never a traceback
        reason = (str(e) if isinstance(e, AuditError)
                  else f"the audit could not finish: {type(e).__name__}: {' '.join(str(e).split())}")
        first, *rest = reason.split("\n")
        print(f"ERROR: {safe_label(first)}", file=sys.stderr)
        for line in rest:
            print(f"  {safe_label(line)}", file=sys.stderr)
        if args.json:
            err = empty_report()
            err.update(status="caller_error", reason=reason, config_dir=args.config_dir)
            print(json.dumps(err, ensure_ascii=False))
        return EXIT_CALLER_ERROR
    print(out)
    disarm_crash_envelope()  # the document is out; a late crash must not add a second
    return EXIT_VIOLATION if has_differences(report) else EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
