#!/usr/bin/env python3
"""check_exporter_capture — a rule pack reads only what its real, pinned exporter serves.

Problem (#3833, #4354)
----------------------
A pack's raw metric names were written against an exporter nobody pinned and
nobody ran. The PostgreSQL pack read `pg_replication_lag`, which
postgres_exporter has not served since its replication queries became a
collector (`pg_replication_lag_seconds`), and `pg_postmaster_start_time_seconds`,
whose collector is off unless a flag turns it on: an alert the platform ships
armed never had data. promtool and pint cannot see this — the expression is
valid; the series is just never there.

What this guards
----------------
A pack whose registry entry carries `exporters:` (rule-packs/threshold-registry.yaml)
names the real exporter it is for: image pinned by tag and digest, the flags
it is started with, the server it watches and the server states to capture.
`scripts/ops/exporter_capture_runtime.sh <pack>` starts exactly that, scrapes
/metrics once per (exporter, state), and calls `--pack <pack> --capture …`
here, which fails unless:

  * every raw selector the pack's rules read — a selector whose metric is under
    the pack's `metric_prefixes`, matchers included — selects at least one
    series of the union of the captures. Whether a selector selects a series
    is Prometheus' own answer (`da-read promql-selectors`), not a re-reading
    of matchers here; a label the series lacks is handed over as "", which is
    what it is to Prometheus. A finding names the rule, the selector, what is missing
    (the family is in no capture / a matcher's label is on no series of the
    family / no series matches) and the captures the family WAS seen in;
  * `A or B`: served when either side is. A side whose family is in NO
    capture must be listed under the pack's `unchecked_alternatives:` with its
    reason — and an entry there that a capture serves, or that no rule reads
    as an alternative, fails too (exit-locked both ways). A side whose family
    IS captured but whose matchers select none of it is a finding, listed or
    not: that is a wrong selector, not an older exporter's name;
  * every capture serves one of the pack's `presence:` selectors (the series
    the liveness rules take as "this exporter is answering");
  * every capture holds its state's `identity:` sample (a replica's scrape
    shows `pg_replication_is_replica 1`), so a scrape of one state filed
    under another's name does not pass for it.

A capture file with a line that is not a text-format sample is exit 2: the
file is not a scrape.

Run with no `--pack`, it is the static half (pre-commit hook
exporter-capture-check): every registry pack is checked
against its real exporter (`exporters:` here, or `queries:` —
check_exporter_queries.py), or is counted as covered another way
(COVERED_ELSEWHERE), or is listed in UNVERIFIED with a reason. A new pack with
none of these fails; a row for a pack that is checked fails. And every pack
with `exporters:` has a driver the runtime script can source.

⚠️ What this does NOT cover
---------------------------
  * Any exporter version, flag set or server version other than the pinned one.
  * Values. A series that is present and always 0 passes.
  * Series that only appear under conditions the captured states do not
    produce. The states are the registry's; what each means is the pack's
    driver (scripts/ops/exporter_capture/<pack>.sh).
  * The labels Prometheus adds at scrape time (`job`, `instance`, `tenant`):
    the capture is the exporter's own /metrics, so a selector matching on one
    of those reads as "no series matches" — a finding, not a pass.

Usage:
  check_exporter_capture.py                 # static half (0 ok / 1 finding / 2 cannot run)
  check_exporter_capture.py --list          # the packs with `exporters:`
  check_exporter_capture.py --plan PACK     # what the runtime script must start
  check_exporter_capture.py --pack PACK --capture 0:primary=FILE [--capture …]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)
import _lib_consumer_read  # noqa: E402  (rule packs and selectors as Prometheus reads them)
import _registry_lib as registry_lib  # noqa: E402
from _lib_exitcodes import Report  # noqa: E402
from _lib_validation import i18n_text  # noqa: E402

_REPO = Path(__file__).resolve().parents[3]
DRIVERS_DIR = _REPO / "scripts" / "ops" / "exporter_capture"
NAME = "check_exporter_capture"

#: Packs counted as covered without a capture of their own, with why. Stated
#: here so that it is a decision someone can read and overturn, not a pack
#: that fell through. (A pack with `queries:` is not listed: its coverage is
#: the registry field itself — exporter_queries_runtime.sh runs its SQL
#: against the real database and compares the served names.)
COVERED_ELSEWHERE: dict[str, str] = {
    "kubernetes": (
        "cAdvisor and kube-state-metrics are scraped by the platform's own jobs, not a "
        "tenant's exporter (no `presence:`); counted as covered by the #3833 pilot's scope "
        "(owner ruling 2026-11-28) — no capture checks these names"),
}

#: ⛔ EXIT-LOCKED (the KNOWN_UNKNOWN_SOURCE discipline of
#: check_scrape_reachability.py). The packs whose raw metric names are still
#: a hand-written list no real exporter was asked about. A pack that gains
#: `exporters:` or `queries:` must leave; a registry pack that is in neither
#: this table, COVERED_ELSEWHERE nor checked is an error. The list can only
#: shrink or stay justified.
_PILOT = ("not yet checked against a real exporter: the PostgreSQL pack is the #3833 pilot "
          "(owner ruling 2026-11-28), the remaining packs wait on its numbers")
UNVERIFIED: dict[str, str] = {
    "mariadb": _PILOT,
    "redis": _PILOT,
    "mongodb": _PILOT + "; #4354 lists two names no percona/mongodb_exporter version serves together",
    "elasticsearch": _PILOT,
    "clickhouse": _PILOT,
    "kafka": _PILOT,
    "rabbitmq": _PILOT,
    "jvm": _PILOT,
    "nginx": _PILOT,
}


class CannotRun(Exception):
    """The check could not be made (unreadable input, a capture that is not one)."""


# ── static half: every pack is checked, covered, or listed ─────────────────

def coverage_problems(doc: dict, unverified: "dict[str, str] | None" = None,
                      covered: "dict[str, str] | None" = None) -> list[str]:
    """Why *doc*'s packs are not each in exactly one of: checked by a capture
    (`exporters:`), checked by its queries file (`queries:`), *covered*,
    *unverified*."""
    unverified = UNVERIFIED if unverified is None else unverified
    covered = COVERED_ELSEWHERE if covered is None else covered
    packs = doc.get("packs") or {}
    problems = []
    for name, meta in packs.items():
        how = [label for label, yes in (
            ("`exporters:`", bool(meta.get("exporters"))),
            ("`queries:`", bool(meta.get("queries"))),
            ("COVERED_ELSEWHERE", name in covered),
            ("UNVERIFIED", name in unverified),
        ) if yes]
        if not how:
            problems.append(
                f"pack {name!r} is not checked against its real exporter: give it `exporters:` "
                f"(and a driver and a CI step), or list it in UNVERIFIED of {NAME}.py with a reason")
        elif len(how) > 1:
            problems.append(
                f"pack {name!r} is under {' and '.join(how)} — a pack that is checked must "
                "leave the ledger (STALE-EXEMPTION)")
    for table, rows in (("UNVERIFIED", unverified), ("COVERED_ELSEWHERE", covered)):
        for name, reason in rows.items():
            if name not in packs:
                problems.append(f"{table} lists {name!r}, which is not a registry pack")
            if not str(reason).strip():
                problems.append(f"{table}[{name!r}] has no reason")
    return problems


def driver_problems(doc: dict, drivers_dir: Path = DRIVERS_DIR) -> list[str]:
    """A pack with `exporters:` and no driver cannot be captured; a driver for
    a pack without them is run by nothing."""
    declared = set(registry_lib.pack_exporters(doc))
    have = {p.stem for p in drivers_dir.glob("*.sh")} if drivers_dir.is_dir() else set()
    return ([f"pack {p!r} has `exporters:` but no driver {drivers_dir.relative_to(_REPO)}/{p}.sh"
             for p in sorted(declared - have)]
            + [f"{drivers_dir.relative_to(_REPO)}/{p}.sh is a driver for a pack without `exporters:`"
               for p in sorted(have - declared)])


# ── the plan the runtime script starts ──────────────────────────────────────

def _entries(doc: dict, pack: str) -> list[dict]:
    entries = registry_lib.pack_exporters(doc).get(pack)
    if not entries:
        raise CannotRun(f"no registry pack {pack!r} with `exporters:`")
    return entries


def capture_ids(entries: list[dict]) -> list[str]:
    """`<index>:<state>` of every capture the entries ask for."""
    return [f"{i}:{state}" for i, e in enumerate(entries) for state in e["states"]]


def plan_lines(doc: dict, pack: str) -> list[str]:
    """What the runtime script must start for *pack*, one `<word> <value>` per
    line: per exporter `exporter <index>`, `name`, `image` and `server` (both
    `<image>:<tag>@<digest>`), a `flag` line per flag, a `state` line per state."""
    lines = []
    for i, e in enumerate(_entries(doc, pack)):
        lines += [f"exporter {i}", f"name {e['name']}", f"image {registry_lib.image_ref(e)}",
                  f"server {registry_lib.image_ref(e['server'])}"]
        lines += [f"flag {f}" for f in e["flags"]]
        lines += [f"state {s}" for s in e["states"]]
    return lines


# ── captures ────────────────────────────────────────────────────────────────

# A label value takes any backslash escape, as Prometheus' text lexer does;
# only \\ \" \n are unescaped (the rest stay as written, as there).
_LABEL_TEXT = r'[a-zA-Z_][a-zA-Z0-9_]*\s*=\s*"(?:[^"\\\n]|\\[^\n])*"'
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\\n]|\\[^\n])*)"')
#: A sample line of the text format, whole: name, an optional label block that
#: holds only `name="value"` pairs (a trailing comma allowed), a value and an
#: optional integer timestamp.
_SAMPLE = re.compile(
    r"([a-zA-Z_:][a-zA-Z0-9_:]*)"
    rf"(\{{\s*(?:{_LABEL_TEXT}\s*(?:,\s*{_LABEL_TEXT}\s*)*,?\s*)?\}})?"
    r"[ \t]+(\S+)(?:[ \t]+-?\d+)?[ \t]*$")
#: The values Prometheus' text parser takes (strconv.ParseFloat): decimal
#: floats with an optional exponent, and NaN / Inf / Infinity in any case,
#: signed. (ParseFloat's hexadecimal and `_`-separated forms are refused: no
#: exporter writes them.)
_VALUE = re.compile(r"[+-]?(?:(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|inf(?:inity)?|nan)",
                    re.IGNORECASE)
_UNESCAPE = {"\\\\": "\\", '\\"': '"', "\\n": "\n"}

def parse_exposition(text: str) -> "list[tuple[str, dict[str, str], float]]":
    """(name, labels, value) of every sample line of a text-format scrape. A
    line that is not one — a label block with anything but `name="value"`
    pairs, a value that is not a number — means the file is not a scrape
    (CannotRun, exit 2), not a series that serves a selector."""
    out = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        m = _SAMPLE.match(line)
        if not m or not _VALUE.fullmatch(m.group(3)):
            raise CannotRun(f"not a /metrics sample line: {line[:120]!r}")
        labels = {k: re.sub(r'\\[\\"n]', lambda e: _UNESCAPE[e.group(0)], v)
                  for k, v in _LABEL.findall(m.group(2) or "")}
        out.append((m.group(1), labels, float(m.group(3))))
    return out


def series_text(name: str, labels: dict[str, str]) -> str:
    """The series as `da-read promql-selectors` takes it: equality matchers,
    values as PromQL double-quoted strings."""
    inner = ",".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in sorted(labels.items()))
    return f"{name}{{{inner}}}"


def read_captures(specs: list[str], want: list[str]) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """capture id -> its samples, for `--capture <id>=<file>` arguments that
    are exactly the captures the registry entry asks for."""
    files: dict[str, Path] = {}
    for spec in specs:
        cid, sep, path = spec.partition("=")
        if not sep or cid in files:
            raise CannotRun(f"--capture {spec!r}: want <exporter index>:<state>=<file>, each once")
        files[cid] = Path(path)
    if sorted(files) != sorted(want):
        raise CannotRun(f"captures given {sorted(files)}, the registry entry asks for {sorted(want)}")
    out = {}
    for cid, path in files.items():
        try:
            samples = parse_exposition(path.read_text(encoding="utf-8"))
        except OSError as e:
            raise CannotRun(f"cannot read the capture {path}: {e}") from e
        if not samples:
            raise CannotRun(f"the capture {path} ({cid}) has no sample line")
        out[cid] = samples
    return out


# ── what the pack reads ─────────────────────────────────────────────────────

def pack_reads(rule_pack: Path) -> list[tuple[str, str]]:
    """(rule name, expr) of every rule of *rule_pack*, as Prometheus loads it."""
    found = _lib_consumer_read.rule_files_in([rule_pack], keep_refusals=True)
    reads = []
    for rfs in found.values():
        for rf in rfs:
            if rf.problem is not None:
                raise CannotRun(f"{rule_pack.name}: {rf.problem}")
            for grp in rf.groups:
                for rule in (grp.get("rules") or []):
                    if isinstance(rule.get("expr"), str):
                        reads.append((str(rule.get("alert") or rule.get("record")), rule["expr"]))
    if not reads:
        raise CannotRun(f"{rule_pack.name} has no rule")
    return reads


def _children(node: dict):
    for value in node.values():
        if isinstance(value, dict) and "kind" in value:
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, dict) and "kind" in v)


def _judged(node: dict, prefixes: tuple[str, ...]) -> bool:
    """A selector this check answers for: the pack's exporter's metric, or one
    with no metric name (which could select it)."""
    name = node.get("name") or ""
    return not name or name.startswith(prefixes)


def _spelling(expr: str, node: dict) -> str:
    return " ".join(expr.encode("utf-8")[node["start"]:node["end"]].decode("utf-8").split())


def _walk(node: dict, served, prefixes):
    """(served, the unserved selectors that make it not served, the unserved
    selectors an `or` excuses) of the subtree *node*. *served(node)* answers
    for one judged selector."""
    if node.get("kind") == "selector":
        if not _judged(node, prefixes) or served(node):
            return True, [], []
        return False, [node], []
    if node.get("kind") == "binary" and node.get("op") == "or":
        (l_ok, l_miss, l_exc), (r_ok, r_miss, r_exc) = (
            _walk(node["lhs"], served, prefixes), _walk(node["rhs"], served, prefixes))
        if l_ok or r_ok:
            # the side without data is an alternative, not a hole
            return True, [], l_exc + r_exc + l_miss + r_miss
        return False, l_miss + r_miss, l_exc + r_exc
    ok, missing, excused = True, [], []
    for child in _children(node):
        c_ok, c_miss, c_exc = _walk(child, served, prefixes)
        ok, missing, excused = ok and c_ok, missing + c_miss, excused + c_exc
    return ok, missing, excused


def _why(node: dict, captures: dict) -> str:
    name = node.get("name") or ""
    seen_in = [cid for cid, samples in captures.items() if any(n == name for n, _, _ in samples)]
    if not name:
        return "no series of any capture matches"
    if not seen_in:
        return f"the family {name} is in no capture ({', '.join(captures)})"
    carried = {k for samples in captures.values() for n, lb, _ in samples if n == name for k in lb}
    absent = sorted({m["name"] for m in node.get("matchers") or []
                     if m["name"] != "__name__" and m["name"] not in carried})
    what = (f"no series of {name} carries the label {', '.join(absent)}" if absent
            else f"no series of {name} matches the matchers")
    return f"{what}; the family was seen in {', '.join(seen_in)}"


def capture_findings(pack: str, doc: dict, captures: dict, *, rule_pack: "Path | None" = None,
                     binary: "str | None" = None) -> list[str]:
    """Findings of *pack* against *captures* (capture id -> samples)."""
    meta = doc["packs"][pack]
    prefixes = tuple(meta.get("metric_prefixes") or ())
    if not prefixes:
        raise CannotRun(f"pack {pack!r} has no metric_prefixes: which selectors are its exporter's?")
    rule_pack = rule_pack or _REPO / meta["rule_pack_file"]
    reads = pack_reads(rule_pack)
    exprs = [e for _, e in reads]
    trees = _lib_consumer_read.promql_shape_each(exprs, binary, raise_refused=True)
    presence = [str(s) for s in meta.get("presence") or []]

    # Which judged selectors does some captured series satisfy? Asked of
    # Prometheus' matcher, one series at a time, until every selector of that
    # family has an answer. A selector is known by where it starts in its
    # expression: da-read reports a range selector without its `[5m]`, the
    # parse tree with it.
    #
    # ⛔ da-read takes a label the series does not name as "of unknown value"
    # and lets any matcher on it pass. A capture is the WHOLE series, and to
    # Prometheus a label a series lacks is the empty string — so every label a
    # judged selector matches on is handed over, as "" where the series has none.
    pending: dict[str, set[tuple[int, int]]] = {}
    matched_on: set[str] = set()
    presence_trees = _lib_consumer_read.promql_shape_each(presence, binary, raise_refused=True)
    for i, tree in enumerate(trees + presence_trees):
        for node in _lib_consumer_read.shape_nodes(tree):
            if node.get("kind") != "selector" or not (i >= len(trees) or _judged(node, prefixes)):
                continue
            matched_on |= {m["name"] for m in node.get("matchers") or []} - {"__name__"}
            if i < len(trees):
                pending.setdefault(node.get("name") or "", set()).add((i, node["start"]))
    # The schema gives every presence selector a metric name.
    presence_names = {re.match(r"[A-Za-z_:][A-Za-z0-9_:]*", s).group(0) for s in presence}
    matched: set[tuple[int, int]] = set()
    presence_in: dict[str, bool] = dict.fromkeys(captures, not presence)
    for cid, samples in captures.items():
        for name, labels, _value in samples:
            wanted = (pending.get(name, set()) | pending.get("", set())) - matched
            need_presence = not presence_in[cid] and name in presence_names
            if not (wanted or need_presence):
                continue
            whole = {**dict.fromkeys(matched_on, ""), **labels}
            hits = _lib_consumer_read.promql_selectors(series_text(name, whole), exprs + presence, binary)
            for i, found in enumerate(hits[:len(exprs)]):
                matched |= {(i, h["start"]) for h in found}
            if any(hits[len(exprs):]):
                presence_in[cid] = True

    out = []
    excused_names: dict[str, str] = {}
    for i, ((rule, expr), tree) in enumerate(zip(reads, trees)):
        _ok, missing, excused = _walk(
            tree, lambda node, i=i: (i, node["start"]) in matched, prefixes)
        for node in missing:
            out.append(f"{rule_pack.name}: rule {rule} reads {_spelling(expr, node)} — "
                       f"{_why(node, captures)}")
        for node in excused:
            # Only a family no capture has is an alternative the pinned
            # exporter does not serve. A family that IS captured but that the
            # selector's matchers do not select is a wrong selector, `or` or not.
            name = node.get("name") or ""
            if name and not any(n == name for samples in captures.values() for n, _, _ in samples):
                excused_names.setdefault(name, rule)
            else:
                out.append(f"{rule_pack.name}: rule {rule} reads {_spelling(expr, node)} as one "
                           f"side of an `or` — {_why(node, captures)}")
    listed = {a["metric"]: a["reason"] for a in meta.get("unchecked_alternatives") or []}
    for name, rule in sorted(excused_names.items()):
        if name not in listed:
            out.append(f"{rule_pack.name}: rule {rule} reads {name} as one side of an `or`, and no "
                       "capture serves it — declare an exporter that does, or list it under the "
                       "pack's unchecked_alternatives with a reason")
    for name in sorted(set(listed) - set(excused_names)):
        out.append(f"unchecked_alternatives lists {name}, which is served by a capture or is not "
                   "read as one side of an `or` — remove the entry (STALE-EXEMPTION)")
    for cid, ok in presence_in.items():
        if not ok:
            out.append(f"capture {cid} serves none of the pack's presence selectors "
                       f"({', '.join(presence)}) — the liveness rules would read this exporter as absent")
    out += identity_findings(registry_lib.pack_exporters(doc).get(pack) or [], captures)
    return out


def identity_findings(entries: list[dict], captures: dict) -> list[str]:
    """A capture is of the state it is filed under: each state with an
    `identity:` in its registry entry must show that sample value (#3833 — a
    primary's scrape filed as the replica's would otherwise pass)."""
    out = []
    for i, entry in enumerate(entries):
        for state, want in (entry.get("identity") or {}).items():
            cid = f"{i}:{state}"
            values = [v for n, _, v in captures.get(cid, []) if n == want["metric"]]
            if float(want["value"]) not in values:
                seen = ", ".join(sorted({f"{v:g}" for v in values})) or "no sample"
                out.append(f"capture {cid} is not of the state {state}: its identity "
                           f"{want['metric']} {want['value']:g} is not in it ({want['metric']}: {seen})")
    return out


# ── main ────────────────────────────────────────────────────────────────────

def main() -> "int | Report":
    ap = argparse.ArgumentParser(description=i18n_text(
        "rule pack 讀的每個原始指標選擇器，都要被 registry 釘住的真實 exporter 的 /metrics 供應"
        "（#3833）。不帶 --pack 時是靜態檢查：每個 pack 要嘛有實測、要嘛列在未驗證清單裡。",
        "Every raw selector a rule pack reads must be served by the /metrics of the real "
        "exporter its registry entry pins (#3833). Without --pack: the static half — every "
        "pack is checked against a real exporter or listed as unverified."))
    ap.add_argument("--ci", action="store_true",
                    help=i18n_text("CI 模式（有發現時 exit 1）", "CI mode (exit 1 on a finding)"))
    ap.add_argument("--json", action="store_true",
                    help=i18n_text("以 JSON 輸出發現", "print findings as JSON"))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help=i18n_text("列出有 exporters: 的 pack", "print the packs with `exporters:`"))
    mode.add_argument("--plan", metavar="PACK",
                      help=i18n_text("印出執行腳本要啟動的東西", "print what the runtime script must start"))
    mode.add_argument("--pack", help=i18n_text("搭配 --capture：要比對的 pack",
                                             "with --capture: the pack to compare"))
    ap.add_argument("--capture", action="append", default=[], metavar="ID=FILE",
                    help=i18n_text("一份 /metrics 抓取結果，ID 是 <exporter 索引>:<狀態>",
                                   "one /metrics scrape; ID is <exporter index>:<state>"))
    ap.add_argument("files", nargs="*",
                    help=i18n_text("忽略（pre-commit 傳入的變更檔）",
                                   "ignored (pre-commit passes changed files)"))
    args = ap.parse_args()
    if bool(args.pack) != bool(args.capture):
        print(f"{NAME}: --pack and --capture go together", file=sys.stderr)
        return 2
    if args.json and (args.list or args.plan):
        # Their output is the runtime script's input, one value per line.
        print(f"{NAME}: --json reports findings; --list and --plan print plain lines", file=sys.stderr)
        return 2
    try:
        doc = registry_lib.committed_registry_doc()
        if args.list:
            print("\n".join(registry_lib.pack_exporters(doc)))
            return 0
        if args.plan:
            print("\n".join(plan_lines(doc, args.plan)))
            return 0
        if args.pack:
            captures = read_captures(args.capture, capture_ids(_entries(doc, args.pack)))
            found = {args.pack: capture_findings(args.pack, doc, captures)}
            what = (f"every raw selector of the {args.pack} pack is served by its pinned exporter "
                    f"({', '.join(captures)})")
        else:
            found = {"registry": coverage_problems(doc) + driver_problems(doc)}
            checked = sorted(registry_lib.pack_exporters(doc))
            what = (f"{len(doc['packs'])} packs: {len(checked)} captured ({', '.join(checked)}), "
                    f"{len(UNVERIFIED)} listed as unverified")
    except (CannotRun, ValueError, OSError, registry_lib.RepoTreeNotFoundError) as e:
        print(f"{NAME}: {e}", file=sys.stderr)
        return 2
    except _lib_consumer_read.ConsumerReadError as e:
        print(f"{NAME}: {_lib_consumer_read.error_text(e)}", file=sys.stderr)
        return 2
    total = sum(len(v) for v in found.values())
    if args.json:
        return Report({"findings": found}, 1 if total else 0)
    if total == 0:
        print(f"{NAME}: OK — {what}.")
    else:
        for scope, items in found.items():
            for item in items:
                print(f"{NAME}: [{scope}] {item}")
    return 1 if total else 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
