#!/usr/bin/env python3
"""migration_audit.py — `da-tools migration-audit`: list where PyYAML and parse-tree differ.

One conf.d read with both YAML readers, differences listed (ADR-036 §8, #2898).

ADR-036 moves every Python reader of conf.d onto `da-guard parse-tree` (Go,
yaml.v3). Before that switch this command shows, on the customer's own tree,
what the switch changes. It reads the tree twice:

* **today** — the route generator's reader: the files
  `_grar_parse._parse_config_files` reads (the shared conf.d walk, one
  defaults carrier per directory), each loaded with the loader it uses
  (`_lib_io.strict_load_exporter_keys`, `raw_text_sequences=("tenants",)`);
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

plus ``selection`` (a file one side reads and the other does not) and
``exporter_changes``: the two readings of the exporter itself that change
when the vendored yaml.v3 "non-specific tag" patch is removed (§8) — a
quoted `! "<<"` key stops being a merge key, and a custom alert's SLO
`objective: ! "99.9"` goes from refused to accepted. parse-tree refuses
both shapes (`non_specific_tag`), so such a file is in
``becomes_unreadable`` too; this list says that the exporter's reading moves
as well. ``still_unreadable`` lists the files both readers refuse; that is
no change, so it does not set the exit code.

Lines are 1-based and come from PyYAML's reading of the file (the key's line
for a mapping entry, the item's line for a sequence item). A value reached
through an alias or a merge is reported at the line of the key that uses
it; a key only yaml.v3 has carries the line of its parent.

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
import subprocess
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
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402
from _lib_io import safe_label, strict_load_exporter_keys  # noqa: E402
from _lib_python import detect_cli_lang  # noqa: E402
import guard_dispatch  # noqa: E402

SCHEMA = "da-tools.migration-audit/v1"
PARSE_TREE_SUBCOMMAND = "parse-tree"
PARSE_TREE_SCHEMA = "da-guard.parse-tree/v1"
DEFAULT_TIMEOUT = 600

# The lists whose entries are differences (exit 1); `still_unreadable` is not.
DIFFERENCE_LISTS = ("selection", "values", "keys", "becomes_unreadable",
                    "becomes_readable", "exporter_changes")
ALL_LISTS = DIFFERENCE_LISTS + ("still_unreadable",)

_NULL_TAG = "tag:yaml.org,2002:null"
# A cap on the nodes one file's comparison visits (aliases can make a small
# file a large tree). Reaching it is listed, never a silent partial pass.
MAX_NODES = 1_000_000


class AuditError(Exception):
    """The audit cannot run (exit 2); the message says why."""


# ── today: the route generator's reading ─────────────────────────────────

def _read_today(path: Path) -> tuple[Optional[Any], Optional[str]]:
    """(data, None) or (None, reason): one file as the generator's read loop
    (`_grar_parse._parse_config_files`) reads it — same open, same loader,
    same whole-file drops. `tests/ops/test_migration_audit.py` holds this to
    the generator's frozen verdict on every row of
    tests/shared/parse_tree_corpus.json."""
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
    """({rel: data} read, {rel: reason} refused) — the files today's route
    generator reads, by the selection of `_grar_parse._parse_config_files`:
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
    """`da-guard parse-tree --config-dir <root>`'s document. Raises
    AuditError when there is no binary, it fails, or its output is not a
    parse-tree document this reader reads — never a partial reading."""
    dispatcher = guard_dispatch.DISPATCHER
    exe = dispatcher.resolve_binary(binary)
    if exe is None:
        raise AuditError(
            dispatcher.binary_missing_message(binary).strip().replace("\n", " ")
            + " This audit reads yaml.v3's reading from `da-guard parse-tree` only; it does "
            "not parse the YAML another way (the da-tools image ships da-guard; in a checkout, "
            "`make da-guard-build` builds .build/da-guard).")
    cmd = [exe, PARSE_TREE_SUBCOMMAND, "--config-dir", str(root)]
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise AuditError(f"da-guard {PARSE_TREE_SUBCOMMAND} did not finish within {timeout}s") from e
    except OSError as e:
        raise AuditError(f"da-guard could not be run ({exe}): {e}") from e
    stderr = proc.stderr.decode("utf-8", errors="backslashreplace").strip()
    if proc.returncode != 0:
        older = ""
        if proc.returncode == 2 and not _accepts(cmd, timeout):
            older = (f" — this da-guard does not know `{PARSE_TREE_SUBCOMMAND}`: it is older "
                     "than this tool; upgrade or rebuild it")
        raise AuditError(f"da-guard {PARSE_TREE_SUBCOMMAND} exited {proc.returncode}{older}"
                         + (f": {' '.join(stderr.split())}" if stderr else ""))
    try:
        doc = json.loads(proc.stdout.decode("utf-8"))
        if not isinstance(doc, dict) or doc.get("schema") != PARSE_TREE_SCHEMA:
            got = doc.get("schema") if isinstance(doc, dict) else None
            raise ValueError(f"schema {got!r}, this reader reads {PARSE_TREE_SCHEMA!r}")
        for field, kind in (("selected", dict), ("files", dict), ("unreadable", list),
                            ("not_read", list)):
            if not isinstance(doc.get(field), kind):
                raise ValueError(f"field {field!r} missing or not a {kind.__name__}")
        # parse-tree's own invariant: "selected" is "files" plus "unreadable".
        unreadable = {u["file"] for u in doc["unreadable"]}
        if set(doc["selected"]) != set(doc["files"]) | unreadable or unreadable & set(doc["files"]):
            raise ValueError("\"selected\" is not \"files\" plus \"unreadable\"")
    except (ValueError, RecursionError) as e:  # UnicodeDecodeError is a ValueError
        # ADR-036 §2: a document a reader cannot decode is unusable whole.
        raise AuditError(f"da-guard {PARSE_TREE_SUBCOMMAND} wrote no document this tool can "
                         f"read ({e}); nothing of it is used") from e
    return doc


def _accepts(cmd: list[str], timeout: float) -> bool:
    """Whether the binary knows the subcommand and its flags (`-h` exits 0)."""
    try:
        proc = subprocess.run([*cmd, "-h"], capture_output=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# ── comparing one file ───────────────────────────────────────────────────

def _is_v3_scalar(v: Any) -> bool:
    # parse-tree: a scalar is the only object whose "text" is a JSON string.
    return isinstance(v, dict) and isinstance(v.get("text"), str) and "tag" in v


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


def suggestion(py: Any, v3: dict) -> str:
    """A spelling both readers read the same way, for a scalar they differ on."""
    text, tag, kind = v3.get("text", ""), v3.get("tag"), _py_kind(py)
    quoted = json.dumps(text, ensure_ascii=False)
    if tag == "!!str" and kind == "bool":
        return (f"if a boolean is meant, write {'true' if py else 'false'}; "
                f"if a string is meant, quote it: {quoted}")
    if tag == "!!str" and kind in ("int", "float"):
        return (f"if a number is meant, write it in decimal: {py!r}; "
                f"if a string is meant, quote it: {quoted}")
    if tag in ("!!int", "!!float") and kind == "str":
        return (f"if a string is meant, quote it: {quoted}; if a number is meant, "
                f"write it in decimal: {v3.get('value') or text}")
    if tag in ("!!int", "!!float") and kind in ("int", "float"):
        return (f"write the number in decimal: {v3.get('value') or text} is what yaml.v3 reads, "
                f"{py!r} what was read until now")
    if tag == "!!timestamp" or kind == "timestamp":
        return f"if a string is meant, quote it: {quoted}"
    return f"quote it if a string is meant ({quoted}); otherwise write it so both read the same"


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
                    self._value(path, line, a, b, suggestion(a, b))
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
                        stack.append((path + [k], a[k], b[k], vn, kline))
                continue
            if isinstance(b, list) and isinstance(a, list) and len(a) == len(b):
                items = node.value if isinstance(node, yaml.SequenceNode) else [None] * len(b)
                for i in range(len(b) - 1, -1, -1):
                    item = items[i] if i < len(items) else None
                    stack.append((path + [i], a[i], b[i], item,
                                  self._line(item) if item is not None else line))
                continue
            self._value(path, line, a, b, "the two readers build a different structure here; "
                        "rewrite it so both read the same")

    def _value(self, path: list, line: Optional[int], a: Any, b: Any, hint: str) -> None:
        self.values.append({"file": self.rel, "line": line, "path": path,
                            "pyyaml": _py_json(a), "yaml_v3": _v3_json(b), "suggestion": hint})


# ── the exporter's own two changes (§8) ──────────────────────────────────

_QUOTED_STYLES = ("'", '"', "|", ">")


def exporter_changes(rel: str, text: str) -> list[dict]:
    """The exporter readings that change when the vendored yaml.v3 patch is
    removed (ADR-036 §8): a quoted or block scalar tagged `!` (the patch
    keeps `!` only there) used as the key `<<` (a merge today, a literal key
    after), or as a custom alert's `slo` → `objective` (refused today,
    accepted after). Read from PyYAML's events of the first document (the
    one yaml.v3 reads); a file PyYAML cannot parse yields nothing here."""
    out: list[dict] = []
    # frames: [kind, path, expecting_key, current_key, index]
    frames: list[list] = []

    def value_path() -> list:
        if not frames:
            return []
        f = frames[-1]
        return f[1] + ([f[3]] if f[0] == "map" else [f[4]])

    def after_node() -> None:
        if frames:
            f = frames[-1]
            if f[0] == "map":
                f[2] = not f[2]
            else:
                f[4] += 1

    try:
        for ev in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(ev, yaml.DocumentEndEvent):
                break
            if isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                frames.pop()
                after_node()
                continue
            if not isinstance(ev, (yaml.ScalarEvent, yaml.AliasEvent,
                                   yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                continue
            is_key = bool(frames) and frames[-1][0] == "map" and frames[-1][2]
            tagged = (isinstance(ev, yaml.ScalarEvent) and ev.tag == "!"
                      and ev.style in _QUOTED_STYLES)
            line = ev.start_mark.line + 1
            if is_key:
                if tagged and ev.value == "<<":
                    out.append({"file": rel, "line": line, "path": frames[-1][1], "change": "merge_key",
                                "today": "a merge key: the mapping it names is merged in",
                                "after": "a literal key \"<<\": nothing is merged"})
                if isinstance(ev, yaml.ScalarEvent):
                    frames[-1][3] = ev.value
                    frames[-1][2] = False
                    continue
            path = value_path()
            if (tagged and len(path) >= 2 and path[-1] == "objective" and path[-2] == "slo"):
                out.append({"file": rel, "line": line, "path": path[:-1], "change": "slo_objective",
                            "today": "refused: the exporter rejects an objective tagged `!`",
                            "after": f"accepted: the string {json.dumps(ev.value)} (!!str)"})
            if isinstance(ev, yaml.MappingStartEvent):
                frames.append(["map", path, True, None, 0])
            elif isinstance(ev, yaml.SequenceStartEvent):
                frames.append(["seq", path, False, None, 0])
            else:
                after_node()
    except (yaml.YAMLError, RecursionError):
        pass
    return out


# ── the audit ────────────────────────────────────────────────────────────

def empty_report() -> dict:
    return {"schema": SCHEMA, "status": "ok", "config_dir": None, "da_guard_version": None,
            "pyyaml_version": yaml.__version__, "summary": {k: 0 for k in ALL_LISTS},
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

    after_selected = {f for f, readers in doc["selected"].items() if readers}
    after_unreadable = {u["file"]: u for u in doc["unreadable"]}
    today_selected = set(today_read) | set(today_refused)

    for f in sorted(after_selected - today_selected):
        report["selection"].append({"file": f, "read_by": "yaml_v3",
                                    "readers": doc["selected"][f],
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
                                     "yaml_v3": None,
                                     "suggestion": f"not compared past {MAX_NODES} nodes; "
                                                   "check this file by hand"})

    # The exporter reads its files whatever parse-tree's verdict.
    for f in sorted(after_selected):
        if "exporter" not in doc["selected"][f]:
            continue
        try:
            text = (root / f).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        report["exporter_changes"].extend(exporter_changes(f, text))

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


def _content_lines(v: Any, indent: str = "      ") -> Iterator[str]:
    if _is_v3_scalar(v):
        yield f"{indent}{_reading(v)}"
        return
    items = v.items() if isinstance(v, dict) else enumerate(v)
    for k, sub in items:
        label = f"- [{k}]" if isinstance(v, list) else f"{k}:"
        if _is_v3_scalar(sub):
            yield f"{indent}{label} {_reading(sub)}"
        else:
            yield f"{indent}{label}" + (" {}" if sub == {} else " []" if sub == [] else "")
            yield from _content_lines(sub, indent + "  ")


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
        ("exporter_changes", "Exporter readings that change when the vendored yaml.v3 patch goes"),
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
            elif key == "becomes_readable":
                lines.append(f"  {s(e['file'])}: today: {s(e['pyyaml_error'])}")
                lines.append("    yaml.v3 reads:" + (" {}" if not e["yaml_v3"] else ""))
                lines.extend(s(ln) for ln in _content_lines(e["yaml_v3"]))
            else:
                lines.append(f"  {s(_where(e))}  {s(_path_text(e['path']))}: {e['change']}")
                lines.append(f"    today: {e['today']}")
                lines.append(f"    after: {s(e['after'])}")
    lines.append("")
    n = sum(report["summary"][k] for k in DIFFERENCE_LISTS)
    lines.append(f"{n} difference(s)" if n else "No differences: both readers read this tree the same way.")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────

# --help in the operator's language (dev-rules §9 L3); report strings stay English.
_HELP = {
    "en": {
        "description": ("Read a conf.d with today's route generator reader (PyYAML) and with "
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
        "description": ("同一棵 conf.d 以今天路由產生器的讀法（PyYAML）與 `da-guard parse-tree`"
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
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=h["timeout"])
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit(Path(args.config_dir), args.da_guard_binary, args.timeout)
    except AuditError as e:
        print(f"ERROR: {safe_label(str(e))}", file=sys.stderr)
        if args.json:
            out = empty_report()
            out.update(status="caller_error", reason=str(e), config_dir=args.config_dir)
            print(json.dumps(out, ensure_ascii=False))
        return EXIT_CALLER_ERROR
    if args.json:
        print(json.dumps(report, ensure_ascii=False, default=str))
    else:
        print(render_text(report))
    return EXIT_VIOLATION if has_differences(report) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
