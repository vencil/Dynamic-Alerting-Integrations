#!/usr/bin/env python3
"""Turn one night's PAIRED benchmark run into per-benchmark ratios (ADR-032).

    python3 scripts/tools/dx/pair_bench_ratio.py \
        --reference .build/bench-ref.txt \
        --main      .build/bench-main.txt \
        --reference-tag exporter/v2.9.0 \
        --out       .build/bench-paired.json

WHAT THIS IS FOR
================
The nightly used to compare tonight's absolute numbers against a MEDIAN OF
OLDER NIGHTS. That reference slides: a permanent regression ages into it and
becomes the new normal, which is how a still-regressed issue gets auto-closed.

ADR-032 replaces it with a ratio measured inside a single night, on a single
machine:

    ratio = median(main side) / median(reference side)

Both sides ran on the same runner in the same job, so the machine's speed
appears in numerator and denominator alike and cancels. This script does only
that arithmetic — it does not decide anything. Deciding is the watchdog's job.

THREE STATES, NOT TWO
=====================
A benchmark that could not be evaluated is NOT the same as a benchmark that was
evaluated and found clean. Conflating them is the exact defect ADR-032 §待決 5
exists to prevent: a missing denominator read as "no findings" makes a
regression silent. So every benchmark lands in exactly one bucket:

    evaluated     both sides measured it → ratio present
    inconclusive  present on one side only → ratio ABSENT + a reason

`missing-in-reference` is the expected state for a benchmark added to main
after the reference version was cut. It is data, not an error, so this script
still exits 0 — but it never lets that benchmark look clean.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import statistics as st
import sys

# Same shape analyze_bench_history.py parses, deliberately: both read the raw
# `go test -bench` stdout that bench_interleave.sh appends per round.
#   "BenchmarkScanDirHierarchical_1000-4   93   35422664 ns/op   ..."
_BENCH_RE = re.compile(
    r"^(Benchmark[A-Za-z0-9_]+)-\d+\s+\d+\s+(\d+(?:\.\d+)?)\s+ns/op\b"
)
_CPU_RE = re.compile(r"^cpu:\s*(\S.*?)\s*$")


def read_side(path: pathlib.Path) -> tuple[dict[str, list[float]], str | None]:
    """Collect every ns/op sample per benchmark, plus the runner's CPU model.

    bench_interleave.sh appends one invocation's stdout per round, so the
    goos/goarch/pkg/cpu headers repeat. That is harmless here for the same
    reason it is harmless in analyze_bench_history.py: one job runs on one
    machine, so the first `cpu:` line is as good as any.
    """
    samples: dict[str, list[float]] = {}
    cpu: str | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            m = _BENCH_RE.match(line)
            if m:
                samples.setdefault(m.group(1), []).append(float(m.group(2)))
                continue
            if cpu is None:
                c = _CPU_RE.match(line)
                if c:
                    cpu = c.group(1)
    return samples, cpu


def pair(ref: dict[str, list[float]],
         main: dict[str, list[float]]) -> tuple[dict, dict]:
    """Split every benchmark seen on either side into evaluated / inconclusive."""
    evaluated: dict[str, dict] = {}
    inconclusive: dict[str, str] = {}
    for bench in sorted(set(ref) | set(main)):
        if bench not in ref:
            inconclusive[bench] = "missing-in-reference"
            continue
        if bench not in main:
            inconclusive[bench] = "missing-in-main"
            continue
        ref_ns = st.median(ref[bench])
        main_ns = st.median(main[bench])
        if ref_ns <= 0:
            inconclusive[bench] = "reference-median-not-positive"
            continue
        evaluated[bench] = {
            "reference_ns": ref_ns,
            "main_ns": main_ns,
            "ratio": main_ns / ref_ns,
            "n_reference": len(ref[bench]),
            "n_main": len(main[bench]),
        }
    return evaluated, inconclusive


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", type=pathlib.Path, required=True,
                    help="bench output of the fixed reference version")
    ap.add_argument("--main", type=pathlib.Path, required=True,
                    help="bench output of main HEAD, same runner, same job")
    ap.add_argument("--reference-tag", required=True,
                    help="the tag the reference side was built from")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    for p in (args.reference, args.main):
        if not p.is_file():
            print(f"[pair_bench_ratio] not a file: {p}", file=sys.stderr)
            return 2

    ref, ref_cpu = read_side(args.reference)
    main_s, main_cpu = read_side(args.main)
    if not ref or not main_s:
        print("[pair_bench_ratio] one side produced no benchmark rows — refusing "
              "to emit a paired verdict", file=sys.stderr)
        return 2

    # Both sides ran in one job on one runner. If the headers disagree, the
    # pairing assumption (machine cancels) is not true and the ratio means
    # nothing — fail loudly rather than publish a number that looks fine.
    if ref_cpu != main_cpu:
        print(f"[pair_bench_ratio] the two sides report different CPUs "
              f"({ref_cpu!r} vs {main_cpu!r}) — that cannot happen within one "
              f"job, so the inputs are not a valid pair", file=sys.stderr)
        return 2

    evaluated, inconclusive = pair(ref, main_s)
    payload = {
        "schema": "bench-paired/v1",
        "reference_tag": args.reference_tag,
        "cpu": ref_cpu,
        "evaluated": evaluated,
        "inconclusive": inconclusive,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")

    print(f"[pair_bench_ratio] reference {args.reference_tag} on {ref_cpu}")
    print(f"[pair_bench_ratio] {len(evaluated)} evaluated, "
          f"{len(inconclusive)} inconclusive → {args.out}")
    for bench, d in sorted(evaluated.items(),
                           key=lambda kv: -abs(kv[1]["ratio"] - 1)):
        print(f"  {100 * (d['ratio'] - 1):+7.2f}%  {bench}")
    for bench, why in sorted(inconclusive.items()):
        print(f"  {'INCONCLUSIVE':>12}  {bench}  ({why})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
