#!/usr/bin/env python3
"""Recompute every number this directory's README claims. No arguments.

    python3 docs/internal/audit-reports/bench-aa-2026-08/analyze_aa.py

Reads only the data files next to it. No network, no benchmark re-runs. It
answers "can the README's numbers be recomputed from the archived data", NOT
"what would a fresh run produce today".

`aa-sessions.json` and `local-benchtime-sweep.tsv` are required;
`aa-multibench-control.tsv` is optional and adds the control section.
"""
import json
import math
import os
import statistics as st

HERE = os.path.dirname(os.path.abspath(__file__))


def aa_sessions():
    doc = json.load(open(os.path.join(HERE, "aa-sessions.json"), encoding="utf-8"))
    for s in doc["sessions"]:
        per = {}
        for name, f in s["files"].items():
            r, side = name[1:].rsplit("_", 1)
            per.setdefault(int(r), {})[side] = f["samples"]
        ratios, ns, first = [], [], []
        for r in sorted(per):
            sides = per[r]
            if "A" not in sides or "B" not in sides:
                continue
            a = st.median(x["ns_op"] for x in sides["A"])
            b = st.median(x["ns_op"] for x in sides["B"])
            ratios.append((r, 100 * (b / a - 1)))
            ns += [x["n"] for v in sides.values() for x in v]
        for name, f in sorted(s["files"].items()):
            v = [x["ns_op"] for x in f["samples"]]
            if len(v) >= 3:
                first.append((name, 100 * (v[0] / st.median(v[1:]) - 1)))
        yield s, ratios, ns, first


def main():
    print("=" * 72)
    print("A/A per-round ratios (true value is exactly 1.000, so every")
    print("deviation is measurement error)")
    pooled = []
    for s, ratios, ns, first in aa_sessions():
        pooled += [(x, s["benchtime"], s["run_id"], r) for r, x in ratios]
        v = [x for _, x in ratios]
        print(f"\n[benchtime={s['benchtime']}] run {s['run_id']} - {s['cpu']}")
        print(f"  note: {s['note']}")
        print("  per-round B/A-1 (%): " + " ".join(f"{x:+.2f}" for x in v))
        print(f"  n={len(v)}  median={st.median(v):+.2f}%  min={min(v):+.2f}%  "
              f"max={max(v):+.2f}%  range={max(v)-min(v):.2f}pp  "
              f"rSD={st.pstdev(v):.2f}pp")
        print(f"  rounds with |ratio-1|>5%: {sum(1 for x in v if abs(x) > 5)}/{len(v)}")
        spread = 100 * (max(ns) - min(ns)) / st.mean(ns)
        print(f"  b.N range: {min(ns)}..{max(ns)} (relative spread {spread:.1f}%)")
        # round(e, 2) is deliberate and is the ONE place where the
        # Chinese->English rewrite changed printed output: the previous version
        # printed the raw float (24.754291585655054) here while every other
        # figure in this file is 2dp. Disclosed because the rewrite's commit
        # message claimed "every printed number is identical" - it was identical
        # in 235 of 236 numbers, not all 236.
        big = [(n, round(e, 2)) for n, e in first if abs(e) > 5]
        print(f"  first sample vs median of the rest: invocations with |excess|>5%: "
              f"{len(big)}/{len(first)}" + (f" -> {big}" if big else ""))

    # This block exists on purpose. The prose quotes the LARGEST ABSOLUTE
    # deviation, while the per-session lines above only give each session's
    # min/max. The first version of the prose therefore missed -3.09%
    # (benchtime=600x, round 4) and claimed "max deviation +2.25%", understating
    # the noise ceiling by 37% - in the direction that makes the instrument look
    # tighter than it measured. Computing the quoted statistic here removes the
    # human step of scanning three tables.
    pooled.sort(key=lambda t: -abs(t[0]))
    worst = pooled[0]
    print(f"\n[all sessions pooled] n={len(pooled)} rounds")
    print(f"  largest |ratio-1| = {abs(worst[0]):.2f}%  ({worst[0]:+.2f}%, "
          f"benchtime={worst[1]}, run {worst[2]}, round {worst[3]})")
    print(f"  most positive = {max(x for x, *_ in pooled):+.2f}%   "
          f"most negative = {min(x for x, *_ in pooled):+.2f}%")
    print(f"  rounds with |ratio-1|>5%: "
          f"{sum(1 for x, *_ in pooled if abs(x) > 5)}/{len(pooled)}")

    print("\n" + "=" * 72)
    print("Local -benchtime=Nx sweep: marginal cost per iteration band")
    rows = []
    for line in open(os.path.join(HERE, "local-benchtime-sweep.tsv"), encoding="utf-8"):
        if line.startswith("#") or line.startswith("bench\t") or not line.strip():
            continue
        b, var, n, ms = line.split("\t")
        rows.append((b, var, int(n), float(ms)))
    for key in sorted({(b, v) for b, v, _, _ in rows}):
        pts = sorted((n, ms) for b, v, n, ms in rows if (b, v) == key)
        print(f"\n{key[0]} [{key[1]}]")
        # `observed-at-3s` is NOT a sweep: those rows are repeated observations
        # of one setting, so the x axis is not "cumulative iterations" and
        # adjacent differencing yields nonsense (measured: -102 ms/iter). It is
        # only used to show the spread of the b.N Go picks.
        if key[1] != "observed-at-3s":
            prev = None
            for n, ms in pts:
                if prev:
                    marg = (n * ms - prev[0] * prev[1]) / (n - prev[0])
                    print(f"  iterations {prev[0]+1:>5}..{n:<5} marginal {marg:7.3f} ms/iter")
                prev = (n, ms)
        d = dict(pts)
        if 400 in d and 800 in d:
            e = math.log(d[800] / d[400]) / math.log(2)
            print(f"  elasticity dln(ns/op)/dln(N) (N=400->800) = {e:+.3f}"
                  f"  => amplification under a fixed benchtime {1/(1-abs(e)):.2f}x")
        if key[1] == "observed-at-3s":
            v = [n for n, _ in pts]
            print(f"  => b.N chosen by Go at benchtime=3s: {v}"
                  f"  relative spread {100*(max(v)-min(v))/(sum(v)/len(v)):.1f}%"
                  f"  (continuous, not discrete tiers)")


def multibench_control():
    """A/A for the default four benchmarks.

    Different benchmark set from aa-sessions.json - never pool the two.
    """
    path = os.path.join(HERE, "aa-multibench-control.tsv")
    if not os.path.exists(path):
        return
    per = {}
    for line in open(path, encoding="utf-8"):
        if line.startswith("#") or line.startswith("round\t") or not line.strip():
            continue
        r, side, bench, n, ns = line.rstrip("\n").split("\t")
        per.setdefault((bench, int(r)), {}).setdefault(side, []).append(float(ns) / 1e6)
    print("\n" + "=" * 72)
    print("Control: default four benchmarks (DIFFERENT benchmark set - do not")
    print("pool with the section above)")
    allv = []
    for bench in sorted({b for b, _ in per}):
        v = []
        for r in sorted(x for bb, x in per if bb == bench):
            s = per[(bench, r)]
            if "A" in s and "B" in s:
                v.append(100 * (st.median(s["B"]) / st.median(s["A"]) - 1))
        if not v:
            continue
        allv += v
        print(f"  {bench:<45} n={len(v)}  rSD {st.pstdev(v):.2f}pp  "
              f"largest |dev| {max(abs(x) for x in v):.2f}%")
    if allv:
        print(f"  four benchmarks pooled n={len(allv)}: rSD {st.pstdev(allv):.2f}pp, "
              f"largest |dev| {max(abs(x) for x in allv):.2f}%, "
              f"|ratio-1|>5% in {sum(1 for x in allv if abs(x) > 5)}/{len(allv)}")


if __name__ == "__main__":
    main()
    multibench_control()
