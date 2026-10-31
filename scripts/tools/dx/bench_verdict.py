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
import sys
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
    args = ap.parse_args(argv)

    try:
        first = _read(args.benchstat)
        if args.cmd == "needs-replication":
            print("yes" if needs_replication(first) else "no")
            return 0
        second = _read(args.replicate) if args.replicate else None
    except ShapeError as exc:
        print(f"::error::benchstat output shape changed — {exc}. The regression "
              "detector may be silently passing; pin benchstat to a known-good "
              "version (see the install note in bench_gate_compare.sh).", file=sys.stderr)
        return 2

    v = decide(first, second)
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
