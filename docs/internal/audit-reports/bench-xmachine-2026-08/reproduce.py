#!/usr/bin/env python3
"""Reproduce every number quoted in ADR-032 §證據 4 from the committed CSVs.

    python3 docs/internal/audit-reports/bench-xmachine-2026-08/reproduce.py

Reads `measurements.csv` + `shard_meta.csv` next to this file. No arguments, no
network, no repo state — if a number in the ADR is not printed here, it is not
supported by this dataset.

WHAT THE DATA IS
================
16 GitHub-hosted runners, each compiling the SAME source and running an
interleaved A/A locally (6 rounds x {A,B}, `-test.count=5`). Because every
measurement anywhere in the matrix comes from identical code, the true ratio
between ANY two of them is exactly 1.000 and every deviation is measurement
error. `shard_meta.csv` carries the per-shard binary SHA-256 so that claim is
checkable rather than assumed — the gate below refuses to report if it fails.

THE TWO ESTIMATORS
==================
    PAIRED   B_j / A_j          same machine     — what ADR-032 proposes
    CROSS    B_j / median(A_k)  k != j, same CPU — what the nightly does today,
                                                   post-#1396 stratification

FAIRNESS — read this before quoting any ratio.
`CROSS` can only be formed for a shard that has >= 2 same-class peers, which
excludes 4 of the 16 runners. Comparing a 16-shard PAIRED against a 12-shard
CROSS changes the sample population as well as the machine identity, so this
script reports PAIRED over BOTH populations and the ADR quotes the matched one.
(External review raised exactly this; the matched figure came out slightly
HIGHER, so the correction did not rescue the conclusion — it sharpened it.)

That exclusion is itself a finding, not an inconvenience: those 4 runners are
the ones the current design cannot evaluate at all, i.e. real-nightly
`INCONCLUSIVE`. Coverage is therefore part of the comparison, not a footnote.
"""
from __future__ import annotations

import collections
import csv
import math
import pathlib
import statistics as st
import sys

HERE = pathlib.Path(__file__).resolve().parent
FLOOR = 0.05          # the nightly's lowest noise floor today
ROUNDS = range(1, 7)


def load(where: pathlib.Path | None = None):
    """Read the dataset. `where` exists so selftest.py can point the SAME
    estimator code at synthetic data with a known planted effect — a validation
    that is worthless if it re-implements the thing it is validating."""
    d = where or HERE
    meta = {}
    with (d / "shard_meta.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            meta[int(r["shard"])] = r
    obs: dict[tuple[int, int, str, str], float] = {}
    with (d / "measurements.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            obs[(int(r["shard"]), int(r["round"]), r["side"], r["bench"])] = \
                float(r["ns_per_op_median"])
    return meta, obs


def estimators(meta, obs):
    """The two estimators, over the MATCHED population. Returns
    (paired_median, cross_median, n_evaluable, n_total)."""
    shards = sorted(meta)
    benches = sorted({k[3] for k in obs})
    med = lambda s, side, b: st.median([obs[(s, r, side, b)] for r in ROUNDS])
    peers = lambda s: [k for k in shards
                       if k != s and meta[k]["cpu"] == meta[s]["cpu"]]
    evaluable = [s for s in shards if len(peers(s)) >= 2]
    paired = [abs(med(j, "B", b) / med(j, "A", b) - 1)
              for j in evaluable for b in benches]
    cross = [abs(med(j, "B", b) /
                 st.median([med(k, "A", b) for k in peers(j)]) - 1)
             for j in evaluable for b in benches]
    return st.median(paired), st.median(cross), len(evaluable), len(shards)


def summarize(label, errs, quiet=False):
    e = sorted(errs)
    p90 = e[math.ceil(0.9 * len(e)) - 1]
    over = sum(1 for x in e if x > FLOOR) / len(e)
    if not quiet:
        print(f"{label:<46}{100*st.median(e):>9.2f}%{100*p90:>9.2f}%"
              f"{100*max(e):>10.2f}%{100*over:>11.1f}%{len(e):>6}")
    return st.median(e)


def main() -> int:
    meta, obs = load()
    shards = sorted(meta)
    benches = sorted({k[3] for k in obs})

    # --- gate: was this an A/A at all? ---------------------------------
    digests = {meta[s]["binary_sha256"] for s in shards}
    print(f"shards {len(shards)}   benches {len(benches)}   "
          f"observations {len(obs)}   distinct binary digests {len(digests)}")
    if len(digests) != 1:
        print("\n*** REFUSING TO REPORT ***\nThe shards did not run identical "
              "binaries, so a deviation from 1.000 is no longer necessarily "
              "measurement error.", file=sys.stderr)
        return 2

    cpus = collections.Counter(meta[s]["cpu"] for s in shards)
    print("\nCPU distribution:")
    for c, n in cpus.most_common():
        print(f"  {n:>2} x {c}")

    med = lambda s, side, b: st.median([obs[(s, r, side, b)] for r in ROUNDS])
    peers = lambda s: [k for k in shards
                       if k != s and meta[k]["cpu"] == meta[s]["cpu"]]
    evaluable = [s for s in shards if len(peers(s)) >= 2]
    excluded = [s for s in shards if s not in evaluable]

    print(f"\nCoverage — the current design needs >=2 same-class peers to form "
          f"a reference:\n  evaluable {len(evaluable)}/{len(shards)} "
          f"({100*len(evaluable)//len(shards)}%);  the other "
          f"{len(excluded)} would be INCONCLUSIVE (shards {excluded})")

    print(f"\n{'estimator (|error| vs true 1.000)':<46}{'median':>9}{'p90':>9}"
          f"{'max':>10}{'>5% floor':>11}{'n':>6}")
    print("-" * 91)
    p_all = summarize("PAIRED  all 16 shards",
                      [abs(med(j, "B", b) / med(j, "A", b) - 1)
                       for j in shards for b in benches])
    p_match = summarize("PAIRED  the same 12 shards (matched)",
                        [abs(med(j, "B", b) / med(j, "A", b) - 1)
                         for j in evaluable for b in benches])
    c_med = summarize("CROSS   vs same-class multi-night median",
                      [abs(med(j, "B", b) /
                           st.median([med(k, "A", b) for k in peers(j)]) - 1)
                       for j in evaluable for b in benches])
    summarize("CROSS   single night vs single night, same class",
              [abs(med(j, "B", b) / med(k, "A", b) - 1)
               for b in benches for j in shards for k in peers(j)])
    summarize("CROSS   single vs single, DIFFERENT class",
              [abs(med(j, "B", b) / med(k, "A", b) - 1)
               for b in benches for j in shards for k in shards
               if meta[k]["cpu"] != meta[j]["cpu"]])

    print(f"\n{'amplification (CROSS / PAIRED)':<46}")
    print(f"  {'matched population (quoted in the ADR)':<44}{c_med/p_match:>6.1f}x")
    print(f"  {'mixed population (do NOT quote)':<44}{c_med/p_all:>6.1f}x")

    print(f"\nPAIRED at reduced interleave depth (cost lever):")
    print(f"{'  rounds used':<46}{'median':>9}{'p90':>9}{'max':>10}"
          f"{'>5% floor':>11}{'n':>6}")
    for n in (6, 3, 1):
        rs = list(ROUNDS)[:n]
        summarize(f"  {n} round(s)",
                  [abs(st.median([obs[(j, r, "B", b)] for r in rs]) /
                       st.median([obs[(j, r, "A", b)] for r in rs]) - 1)
                   for j in shards for b in benches])

    print("\nPer-CPU-class medians (ms/op) — shows the machine term directly:")
    short = lambda c: (c.replace("Processor", "").replace("Intel(R) ", "")
                        .replace("INTEL(R) ", "").replace("Xeon(R) ", "")
                        .replace("XEON(R) ", "").replace("AMD ", "").strip())
    order = [c for c, _ in cpus.most_common()]
    print(f"{'bench':<40}" + "".join(f"{short(c)[:11]:>12}" for c in order)
          + f"{'slow/fast':>11}")
    for b in benches:
        cell, vals = "", {}
        for c in order:
            xs = [med(s, "A", b) for s in shards if meta[s]["cpu"] == c]
            vals[c] = st.median(xs)
            cell += f"{vals[c]/1e6:>12.3f}"
        print(f"{b:<40}{cell}{max(vals.values())/min(vals.values()):>10.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
