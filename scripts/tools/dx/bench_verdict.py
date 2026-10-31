#!/usr/bin/env python3
"""Turn a benchstat comparison into the bench gate's verdict (shared core).

    # Does this comparison need a second interleaved pass before it is judged?
    python3 scripts/tools/dx/bench_verdict.py needs-replication --benchstat first.txt

    # Judge it (optionally against the second pass) and write step outputs.
    python3 scripts/tools/dx/bench_verdict.py verdict \
        --benchstat first.txt [--replicate second.txt] \
        --github-output "$GITHUB_OUTPUT"

WHO CALLS THIS
==============
Only `scripts/tools/ops/bench_gate_compare.sh`, which is itself shared by the
PR gate, post-merge attribution and the /bench on-demand workflow. This module
is the ONE place the gate reads benchstat's text: regression rows, the control
canary, the CPU headers and the output-shape assertions all live here, so the
three consumers cannot disagree about what a run said.

WHY THE UNIT MATTERS
====================
benchstat prints one section per unit (`sec/op`, `B/op`, `allocs/op`) and the
data rows do not repeat the unit. The gate used to grep rows without tracking
the section, so a +6% `sec/op` row and a +30% `allocs/op` row were the same
kind of evidence. They are not:

  * `allocs/op` and `B/op` count work. Both sides run the same binary input,
    so they are identical run to run — a flagged row means the code does more.
  * `sec/op` is a wall-clock reading on a shared, heterogeneous GitHub-hosted
    runner. The interleaved topology and the control canary remove most drift,
    not all of it: four post-merge attribution issues (#2324, #2628, #2806,
    #3401) flagged only `sec/op`, and re-measuring each one showed B/op and
    allocs/op byte-identical between parent and commit.

A timing-only flag is still sometimes real (#3262 was +19% `sec/op` with no
allocation change, and was fixed). So it is not dropped: it is REPLICATED. The
caller runs a second, independent interleaved pass and a timing-only flag
stands only for a benchmark that both passes flag. Requiring two independent
passes to agree takes a per-benchmark false-positive rate of about α to about
α², while a regression large enough to clear the 5% floor once clears it again.

THREE STATES, NOT TWO
=====================
`inconclusive` (the canary drifted, is missing, or the CPUs differ) is never a
clean result. If the SECOND pass is inconclusive, the timing-only flag can be
neither confirmed nor dismissed, so the whole verdict is inconclusive rather
than quietly clean.

EXIT CODES
==========
0  a verdict was produced (including "regression detected" — policy is the
   caller's job)
2  the input could not be judged: missing file, or benchstat output whose shape
   this parser does not recognise. Fail loud — an unparsed comparison must never
   read as "no regression".
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Same two sys.path inserts every sibling dx tool uses: parent for the repo
# layout where _lib_compat.py sits one directory up, self-dir for the flat
# Docker layout. Importing the carrier is what hardens stdout — without it
# `--help` can die on a legacy Windows console (cp950).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# Gate thresholds. Policy lives here once; bench_gate_compare.sh only orchestrates.
MAGNITUDE_FLOOR_PCT = 5.0
CPU_CANARY_FLOOR_PCT = 4.0

DETERMINISTIC_UNITS = frozenset({"B/op", "allocs/op"})
TIMING_UNIT = "sec/op"
UNITS = DETERMINISTIC_UNITS | {TIMING_UNIT}

CANARY_GATING = "ControlCanaryCPU"
CANARY_PREFIX = "ControlCanary"

_UNIT_HEADER = re.compile(r"│\s*(sec/op|B/op|allocs/op)\b")
# benchstat prints a significant delta as "+5.35% (p=0.004 n=6)" and an
# insignificant one as "~ (p=0.485 n=6)". Only the signed form is a finding.
_DELTA = re.compile(r"(?<![\w.])([+-]\d+\.\d+)%\s*\(p=")
# A significant change FROM ZERO has no percentage, so benchstat prints
# "? (p=0.002 n=6)" — e.g. a path that did not allocate and now does
# (0 → 48 B/op). Read it as an unbounded increase: it is the clearest
# deterministic regression there is, and the old shell grep missed it too.
# (Going TO zero prints "-100.00%", an ordinary improvement.)
_FROM_ZERO = re.compile(r"(?<![\w.])\?\s*\(p=")
_RESULT_ROW = re.compile(r"±\s*\d+%")


class ShapeError(Exception):
    """benchstat output this parser cannot vouch for."""


@dataclass(frozen=True)
class Row:
    bench: str
    unit: str
    delta_pct: float
    line: str


@dataclass
class Reading:
    """Everything one benchstat comparison says, parsed once."""

    regressions: list[Row] = field(default_factory=list)
    canary_rows: list[str] = field(default_factory=list)
    canary_drift: list[Row] = field(default_factory=list)
    canary_seen: bool = False
    cpus: list[str] = field(default_factory=list)

    @property
    def deterministic(self) -> list[Row]:
        return [r for r in self.regressions if r.unit in DETERMINISTIC_UNITS]

    @property
    def timing(self) -> list[Row]:
        return [r for r in self.regressions if r.unit == TIMING_UNIT]

    def inconclusive_reason(self) -> str:
        if len(self.cpus) > 1:
            return ("Heterogeneous CPUs in a single interleaved job — "
                    "investigate runner provisioning.")
        if self.canary_drift:
            return (f"CPU control canary drifted ≥ {CPU_CANARY_FLOOR_PCT:g}% — this run's "
                    "runner was unstable (thermal/frequency/neighbour). Comparison "
                    "untrustworthy; re-run.")
        if not self.canary_seen:
            return ("Control canary absent from output — cannot verify runner "
                    "stability this run. Re-run.")
        return ""


def parse(text: str) -> Reading:
    """Parse benchstat's text output. Raises ShapeError on unrecognised shape."""
    if not any(u in text for u in UNITS):
        raise ShapeError("no metric-section header (sec/op / B/op / allocs/op) in the "
                         "comparison")
    if not _RESULT_ROW.search(text):
        raise ShapeError("no per-bench result row (matching '± N%') in the comparison")

    reading = Reading()
    unit: str | None = None
    for line in text.splitlines():
        if line.startswith("cpu:"):
            cpu = line[len("cpu:"):].strip()
            if cpu not in reading.cpus:
                reading.cpus.append(cpu)
            continue
        if line.startswith("pkg:"):
            unit = None  # a new package starts a new set of unit sections
            continue
        header = _UNIT_HEADER.search(line)
        if header:
            unit = header.group(1)
            continue
        if not line or line[0].isspace() or line.startswith(("geomean", "goos:", "goarch:")):
            continue
        name = line.split()[0]
        if name.startswith(CANARY_PREFIX):
            reading.canary_rows.append(line)  # shown when the run is inconclusive
            if name.startswith(CANARY_GATING):
                reading.canary_seen = True
        delta = _DELTA.search(line)
        if delta:
            pct = float(delta.group(1))
        elif _FROM_ZERO.search(line):
            pct = float("inf")
        else:
            continue
        if unit is None:
            raise ShapeError(f"a result row sits outside any unit section: {line!r}")
        row = Row(bench=name, unit=unit, delta_pct=pct, line=line)
        if name.startswith(CANARY_PREFIX):
            # Sign-agnostic: head unfairly favoured disqualifies the run too.
            # The Sleep canary is informational only (µs scheduler jitter).
            if name.startswith(CANARY_GATING) and abs(row.delta_pct) >= CPU_CANARY_FLOOR_PCT:
                reading.canary_drift.append(row)
            continue  # environment probes, never product regressions
        if row.delta_pct >= MAGNITUDE_FLOOR_PCT:
            reading.regressions.append(row)
    return reading


# ── Acceptance ledger ───────────────────────────────────────────────────────
# A regression someone decided to pay for is declared in the SAME PR that
# introduces it, in LEDGER_PATH. The gate reads the entries that PR (or push)
# ADDED — the set difference between the ledger at the base and at the head,
# compared entry by entry, so reformatting the file re-accepts nothing — and a
# flagged row is "accepted" only when an added entry names that benchmark,
# that unit, and a bound the measurement does not exceed. Over the bound it is
# still a regression, reported with both numbers. Decision record: ADR-039.
LEDGER_PATH = ".github/bench-accepted.toml"
_ENTRY_KEYS = {"benches", "metric", "max_delta_pct", "reason", "issue"}
_REQUIRED_KEYS = _ENTRY_KEYS - {"issue"}
_GO_BENCH_NAME = re.compile(r"^Benchmark[A-Za-z0-9_]+$")


class LedgerError(Exception):
    """A ledger this gate cannot apply. Fail loud: ignoring it would turn a
    declared acceptance into a ticket, or worse, an invalid entry into a pass."""


@dataclass(frozen=True)
class Entry:
    """Identity = what is accepted: the benchmark SET, the metric, the bound.
    Reordering names or rewording `reason` / adding `issue` is the same entry
    and must not re-accept anything; widening the bound is a new acceptance."""
    benches: tuple[str, ...]  # sorted, so order inside the list is not identity
    metric: str
    max_delta_pct: float
    reason: str = field(compare=False)
    issue: int | None = field(default=None, compare=False)


def parse_ledger(text: str, where: str = LEDGER_PATH) -> list[Entry]:
    """Parse and validate the ledger. Raises LedgerError on any malformed entry."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LedgerError(f"{where}: not valid TOML: {exc}") from exc
    extra = set(doc) - {"accepted"}
    if extra:
        raise LedgerError(f"{where}: unknown top-level key(s) {sorted(extra)}")
    raw = doc.get("accepted", [])
    if not isinstance(raw, list):
        raise LedgerError(f"{where}: `accepted` must be an array of tables")
    entries: list[Entry] = []
    for i, e in enumerate(raw, 1):
        at = f"{where}: accepted entry #{i}"
        if not isinstance(e, dict):
            raise LedgerError(f"{at}: not a table")
        unknown = set(e) - _ENTRY_KEYS
        missing = _REQUIRED_KEYS - set(e)
        if unknown or missing:
            raise LedgerError(f"{at}: unknown {sorted(unknown)} / missing {sorted(missing)}")
        benches = e["benches"]
        if (not isinstance(benches, list) or not benches
                or not all(isinstance(b, str) and _GO_BENCH_NAME.match(b) for b in benches)):
            raise LedgerError(f"{at}: `benches` must be a non-empty list of full Go "
                              "benchmark names (BenchmarkX), not patterns")
        if e["metric"] not in UNITS:
            raise LedgerError(f"{at}: `metric` must be one of {sorted(UNITS)}")
        cap = e["max_delta_pct"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not cap > 0:
            raise LedgerError(f"{at}: `max_delta_pct` must be a number > 0")
        if not isinstance(e["reason"], str) or not e["reason"].strip():
            raise LedgerError(f"{at}: `reason` must be a non-empty string")
        issue = e.get("issue")
        if issue is not None and (isinstance(issue, bool) or not isinstance(issue, int)):
            raise LedgerError(f"{at}: `issue` must be an issue number")
        entries.append(Entry(tuple(sorted(set(benches))), e["metric"], float(cap),
                             e["reason"].strip(), issue))
    return entries


def ledger_at(sha: str) -> str | None:
    """The ledger's text at a commit; None when the file does not exist there."""
    if subprocess.run(["git", "rev-parse", "--verify", "-q", f"{sha}^{{commit}}"],
                      capture_output=True, timeout=30).returncode != 0:
        raise LedgerError(f"ledger commit does not resolve: {sha}")
    if subprocess.run(["git", "cat-file", "-e", f"{sha}:{LEDGER_PATH}"],
                      capture_output=True, timeout=30).returncode != 0:
        return None
    return subprocess.run(["git", "show", f"{sha}:{LEDGER_PATH}"], capture_output=True,
                          text=True, encoding="utf-8", timeout=30, check=True).stdout


def added_entries(base_sha: str, head_sha: str) -> list[Entry]:
    """Entries present at head and not at base."""
    def at(sha: str) -> list[Entry]:
        text = ledger_at(sha)
        return parse_ledger(text, f"{LEDGER_PATH}@{sha[:12]}") if text is not None else []
    before = set(at(base_sha))
    return [e for e in at(head_sha) if e not in before]


def go_name(row_bench: str) -> str:
    """benchstat row name ("MergePartialConfigs_1000-4") → Go name."""
    return "Benchmark" + re.sub(r"-\d+$", "", row_bench)


@dataclass(frozen=True)
class Judged:
    row: Row
    entry: Entry


def apply_ledger(reading: Reading, entries: list[Entry]) -> tuple[list[Judged], list[Judged]]:
    """Remove covered rows from `reading.regressions`; return (accepted, over_bound)."""
    accepted: list[Judged] = []
    over: list[Judged] = []
    remaining: list[Row] = []
    for row in reading.regressions:
        matches = [e for e in entries if e.metric == row.unit and go_name(row.bench) in e.benches]
        if not matches:
            remaining.append(row)
            continue
        widest = max(matches, key=lambda e: e.max_delta_pct)
        if row.delta_pct <= widest.max_delta_pct:
            accepted.append(Judged(row, widest))
        else:
            remaining.append(row)
            over.append(Judged(row, widest))
    reading.regressions = remaining
    return accepted, over


def lint_ledger(entries: list[Entry], definitions: dict[str, int]) -> list[str]:
    """Problems a ledger that parses can still have. `definitions` maps each
    benchmark name to how many places define it. A name defined nowhere was
    renamed or removed (the entry silently matches nothing); a name defined in
    more than one package is ambiguous, since benchstat rows carry no package."""
    problems = []
    for i, e in enumerate(entries, 1):
        for b in e.benches:
            n = definitions.get(b, 0)
            if n == 0:
                problems.append(f"accepted entry #{i}: no benchmark named {b}")
            elif n > 1:
                problems.append(f"accepted entry #{i}: {b} is defined in {n} places")
    return problems


def needs_replication(first: Reading) -> bool:
    """True when the only evidence is timing rows from a stable run."""
    return (not first.inconclusive_reason()
            and not first.deterministic
            and bool(first.timing))


@dataclass
class Verdict:
    regression: bool
    regressions: list[Row]
    inconclusive_reason: str
    basis: str
    unreplicated: list[Row] = field(default_factory=list)
    canary_rows: list[str] = field(default_factory=list)
    cpus: list[str] = field(default_factory=list)
    accepted: list[Judged] = field(default_factory=list)
    over_bound: list[Judged] = field(default_factory=list)


def decide(first: Reading, second: Reading | None) -> Verdict:
    """Combine the first pass with an optional replication pass."""
    common = {"canary_rows": first.canary_rows, "cpus": first.cpus}
    reason = first.inconclusive_reason()
    if reason:
        # Rows are still reported so the summary can show them, but every
        # consumer reads `inconclusive` first and treats the run as "re-run".
        return Verdict(bool(first.regressions), first.regressions, reason,
                       "inconclusive", **common)
    if first.deterministic:
        return Verdict(True, first.regressions, "", "deterministic", **common)
    if not first.timing:
        return Verdict(False, [], "", "clean", **common)
    if second is None:
        return Verdict(True, first.timing, "", "timing-unreplicated", **common)

    second_reason = second.inconclusive_reason()
    if second_reason:
        return Verdict(False, [], "Replication pass: " + second_reason,
                       "inconclusive", unreplicated=first.timing,
                       canary_rows=second.canary_rows or first.canary_rows,
                       cpus=sorted(set(first.cpus) | set(second.cpus)))
    again = {r.bench for r in second.timing}
    confirmed = [r for r in first.timing if r.bench in again]
    dismissed = [r for r in first.timing if r.bench not in again]
    if confirmed:
        return Verdict(True, confirmed, "", "timing-replicated",
                       unreplicated=dismissed, **common)
    return Verdict(False, [], "", "timing-not-replicated", unreplicated=dismissed, **common)


def _multiline(key: str, value: str, delim: str) -> str:
    return f"{key}<<{delim}\n{value}\n{delim}\n"


def render_outputs(v: Verdict, benchstat_path: str) -> str:
    """GitHub step outputs, the keys bench_gate_compare.sh documents."""
    lines = "\n".join(r.line for r in v.regressions)
    out = [
        f"inconclusive={'true' if v.inconclusive_reason else 'false'}\n",
        _multiline("inconclusive_reason", v.inconclusive_reason, "__REOF__"),
        f"benchstat_txt={benchstat_path}\n",
        f"regression_basis={v.basis}\n",
        f"regression={'true' if v.regression else 'false'}\n",
    ]
    if v.regression:
        out.append(_multiline("regressions", lines, "__EOF__"))
    if v.unreplicated:
        out.append(_multiline("unreplicated", "\n".join(r.line for r in v.unreplicated),
                              "__UNREPEOF__"))
    if v.accepted:
        out.append(_multiline("accepted", "\n".join(
            f"{j.row.line}  [declared ≤ +{j.entry.max_delta_pct:g}%: {j.entry.reason}]"
            for j in v.accepted), "__ACCEOF__"))
    if v.over_bound:
        out.append(_multiline("over_bound", "\n".join(
            f"{j.row.line}  [declared ≤ +{j.entry.max_delta_pct:g}% in {LEDGER_PATH}]"
            for j in v.over_bound), "__OVEREOF__"))
    if v.inconclusive_reason and v.canary_rows:
        out.append(_multiline("inconclusive_canary", "\n".join(v.canary_rows), "__CANEOF__"))
    if len(v.cpus) > 1:
        out.append(_multiline("inconclusive_cpus", "\n".join(v.cpus), "__CPUEOF__"))
    return "".join(out)


def _read(path: str) -> Reading:
    p = Path(path)
    if not p.is_file():
        raise ShapeError(f"benchstat output not found: {path}")
    return parse(p.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    nr = sub.add_parser("needs-replication",
                        help="print 'yes' when only timing rows were flagged")
    nr.add_argument("--benchstat", required=True)
    vd = sub.add_parser("verdict", help="judge the comparison and write step outputs")
    vd.add_argument("--benchstat", required=True)
    vd.add_argument("--replicate", help="benchstat output of the replication pass")
    vd.add_argument("--github-output", required=True,
                    help="file to append GitHub step outputs to")
    for p in (nr, vd):
        p.add_argument("--ledger-base", help=f"commit whose {LEDGER_PATH} is the 'before' set")
        p.add_argument("--ledger-head", help=f"commit whose {LEDGER_PATH} is the 'after' set")
    args = ap.parse_args(argv)
    if bool(args.ledger_base) != bool(args.ledger_head):
        print("::error::--ledger-base and --ledger-head go together", file=sys.stderr)
        return 2

    try:
        entries = (added_entries(args.ledger_base, args.ledger_head)
                   if args.ledger_base else [])
        first = _read(args.benchstat)
        accepted, over = apply_ledger(first, entries)
        if args.cmd == "needs-replication":
            print("yes" if needs_replication(first) else "no")
            return 0
        # The second pass answers only "is it flagged again?". It is NOT
        # filtered by the ledger: a row over its bound in the first pass that
        # lands inside the bound in the second was still flagged again, and
        # hiding it would turn an over-bound regression into "not replicated".
        second = _read(args.replicate) if args.replicate else None
    except ShapeError as exc:
        print(f"::error::benchstat output shape changed — {exc}. The regression "
              "detector may be silently passing; pin benchstat to a known-good "
              "version (see the install note in bench_gate_compare.sh).", file=sys.stderr)
        return 2
    except (LedgerError, subprocess.SubprocessError) as exc:
        print(f"::error::cannot apply {LEDGER_PATH}: {exc}", file=sys.stderr)
        return 2

    v = decide(first, second)
    counted = set(v.regressions)
    v.accepted = accepted
    v.over_bound = [j for j in over if j.row in counted]  # only what the verdict counts
    if v.basis == "clean" and accepted:
        v.basis = "accepted"
    with open(args.github_output, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(render_outputs(v, args.benchstat))
    if v.inconclusive_reason:
        print(f"::warning::{v.inconclusive_reason}", file=sys.stderr)
    if v.basis == "timing-not-replicated":
        print("::notice::timing-only flag did not replicate in a second interleaved "
              "pass — not counted as a regression:", file=sys.stderr)
        for r in v.unreplicated:
            print(f"  {r.line}", file=sys.stderr)
    print(f"[bench_verdict] verdict: regression={str(v.regression).lower()} "
          f"inconclusive={str(bool(v.inconclusive_reason)).lower()} basis={v.basis}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
