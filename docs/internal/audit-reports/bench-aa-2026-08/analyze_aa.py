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


def warmup_fit(pts):
    """One-parameter warm-up model for the baseline curves.

    Added 2026-09-01 (issue #1497). The instrumented branch count on the flat
    scanner shows the cost curve is not mysterious: for the first W iterations
    every one of the 1000+ files misses the mtime guard and is re-hashed, after
    which exactly one file does. So ns/op should be

        t(N) = t_steady + C * min(N, W) / N

    with t_steady taken from the largest N measured and (C, W) fitted here on
    N>=100. Small N is excluded: those points are single-digit-iteration
    invocations whose own noise dominates. Prints nothing when the curve is too
    short to fit.
    """
    d = dict(pts)
    ns = [n for n in sorted(d) if n >= 100]
    if len(ns) < 4:
        return
    t_steady = d[max(d)]
    best = None
    for w in range(20, max(ns) + 1, 5):
        num = sum((d[n] - t_steady) * (min(n, w) / n) for n in ns)
        den = sum((min(n, w) / n) ** 2 for n in ns)
        if den == 0:
            continue
        c = num / den
        err = [100 * ((t_steady + c * min(n, w) / n) / d[n] - 1) for n in ns]
        rms = (sum(e * e for e in err) / len(err)) ** 0.5
        if best is None or rms < best[0]:
            best = (rms, w, c, max(abs(e) for e in err))
    rms, w, c, worst = best
    # A flat curve admits any W, so the fitted W is meaningless there. Report
    # the absence of a transient instead of a spurious 800-iteration one - the
    # `backdated` variant is exactly this case and is the positive control.
    if abs(c) < 0.05 * t_steady:
        print(f"  warm-up model: no transient detected (fitted excess {c:+.3f} ms/iter"
              f" is under 5% of the {t_steady:.3f} ms steady state) - curve is flat")
        return
    print(f"  warm-up model t(N) = {t_steady:.3f} + {c:.3f}*min(N,{w})/N ms"
          f"  [fitted on N>={ns[0]}: rms {rms:.1f}%, max {worst:.1f}%]")
    print(f"    => warm-up costs {c:.3f} ms/iter extra and lasts ~{w} iterations"
          f" (~{w * (t_steady + c) / 1000:.2f} s of loop time, consistent with the"
          f" 2 s mtime guard once setup before the loop is counted)")


def bn_gap_effect():
    """How much of the A/A ratio can the two sides' differing b.N account for?

    Added 2026-09-01 (issue #1497). The two sides of a paired round do not land
    on the same b.N, and ns/op depends on N, so a gap contributes to the ratio
    on its own. This bounds that contribution: take the per-round gap between
    the two sides' b.N, and read the curve for what a gap that size is worth.

    ONLY the sessions whose benchmark the local sweep also covers are used -
    reading one benchmark's operating point off another's curve is the error
    this whole directory exists to prevent.
    """
    rows = []
    for line in open(os.path.join(HERE, "local-benchtime-sweep.tsv"), encoding="utf-8"):
        if line.startswith("#") or line.startswith("bench\t") or not line.strip():
            continue
        b, var, n, ms = line.split("\t")
        if var == "baseline":
            rows.append((b, int(n), float(ms)))
    print("\n" + "=" * 72)
    print("b.N gap between the two sides: how much of the A/A ratio can it explain")
    for s, ratios, ns, _ in aa_sessions():
        bench = sorted({x["bench"] for v in s["files"].values() for x in v["samples"]})
        curve = {n: ms for b, n, ms in rows if "Benchmark" + b == bench[0]}
        per = {}
        for name, f in s["files"].items():
            r, side = name[1:].rsplit("_", 1)
            per.setdefault(int(r), {})[side] = st.median(x["n"] for x in f["samples"])
        gaps = [abs(v["B"] - v["A"]) for v in per.values() if "A" in v and "B" in v]
        med = st.median(ns)
        print(f"\n[benchtime={s['benchtime']}] run {s['run_id']}  {bench[0]}")
        if not gaps or max(gaps) == 0:
            print("  b.N identical on both sides in every round - gap contributes exactly 0")
            continue
        print(f"  per-round |b.N gap|: median {st.median(gaps):.1f}  max {max(gaps):.1f}"
              f"  (median b.N {med:.0f})")
        if len(curve) < 2:
            print("  no local sweep for this benchmark - cannot price the gap")
            continue
        lo, hi = _curve_at(curve, med), _curve_at(curve, med + max(gaps))
        print(f"  the LARGEST gap is worth {100 * (hi / lo - 1):+.2f}% on the local curve"
              f"  (N={med:.0f} -> {med + max(gaps):.0f})")
        print(f"  observed A/A spread this session: "
              f"{max(x for _, x in ratios) - min(x for _, x in ratios):.2f}pp")


def _curve_at(curve, n):
    """log-log interpolation on the swept points; clamps outside the range."""
    ks = sorted(curve)
    n = min(max(n, ks[0]), ks[-1])
    for a, b in zip(ks, ks[1:]):
        if a <= n <= b:
            w = (math.log(n) - math.log(a)) / (math.log(b) - math.log(a))
            return math.exp(math.log(curve[a]) + w * (math.log(curve[b]) - math.log(curve[a])))
    return curve[ks[-1]]


def main():
    print("=" * 72)
    print("A/A per-round ratios (true value is exactly 1.000, so every")
    print("deviation is measurement error)")
    pooled, all_ns, bn_benches = [], [], set()
    for s, ratios, ns, first in aa_sessions():
        pooled += [(x, s["benchtime"], s["run_id"], r) for r, x in ratios]
        # Only sessions with a TIME-based benchtime tell us what b.N Go picks
        # on its own. The `600x` session had b.N pinned by the operator, so
        # folding its 600 into this range would describe the experiment, not
        # the nightly.
        if not s["benchtime"].endswith("x"):
            all_ns += ns
            bn_benches.update(x["bench"] for v in s["files"].values()
                              for x in v["samples"])
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

    bn_range = (min(all_ns), max(all_ns)) if all_ns else None

    print("\n" + "=" * 72)
    print("Local -benchtime=Nx sweep: marginal cost per iteration band")
    if bn_range:
        print(f"Go picks b.N in {bn_range[0]}..{bn_range[1]} under a time-based benchtime")
        print("(pooled from the sessions above; the `600x` session is excluded because")
        print(" its b.N was pinned by the operator).")
        # ONLY for the benchmark those sessions actually ran. b.N is per
        # benchmark - a cheaper one gets a larger b.N out of the same benchtime
        # - so flagging another curve against this range would be reading one
        # benchmark's operating point off another's.
        print(f" It is the range for {', '.join(sorted(bn_benches))} and is used to")
        print(" flag spans on that curve only.")
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
        # Same guard as the marginal block above: `observed-at-3s` is not a
        # sweep, so neither an elasticity nor a warm-up fit means anything on
        # it (it produced e=-14.190 across its 421->422 pair before this guard).
        if key[1] != "observed-at-3s":
            d = dict(pts)
            in_range_bench = bn_range and "Benchmark" + key[0] in bn_benches
            # CORRECTION (2026-09-01, issue #1497). The original printed ONLY the
            # N=400->800 span and the README quoted its 1.26x. That span is not
            # where the nightly operates: the b.N Go actually picks is printed just
            # above (279..441 in the archived sessions), and this curve is markedly
            # steeper below 400. The 400->800 figure therefore UNDERSTATED the
            # amplification. Every adjacent span is printed now; the old one is
            # marked, not deleted, so the README's original number stays locatable.
            for lo_n, hi_n in zip(sorted(d), sorted(d)[1:]):
                # Below 100 iterations a single invocation's own noise dominates
                # the span (see the 2..5 band above at 13.7 ms/iter), so those
                # elasticities describe the noise, not the curve.
                if lo_n < 100:
                    continue
                e = math.log(d[hi_n] / d[lo_n]) / math.log(hi_n / lo_n)
                mark = ""
                if (lo_n, hi_n) == (400, 800):
                    mark = "   <- the span the first version of this script printed"
                elif in_range_bench and any(lo_n <= v <= hi_n for v in bn_range):
                    mark = f"   <- contains the observed b.N ({bn_range[0]}..{bn_range[1]})"
                print(f"  elasticity dln(ns/op)/dln(N) (N={lo_n}->{hi_n}) = {e:+.3f}"
                      f"  => amplification under a fixed benchtime {1/(1-abs(e)):.2f}x{mark}")
            warmup_fit(pts)
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
    bn_gap_effect()
    multibench_control()
