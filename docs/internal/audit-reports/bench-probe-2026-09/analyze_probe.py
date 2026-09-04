#!/usr/bin/env python3
"""Recompute every number in this directory's README from the archived PROBE records.

⛔ It recomputes; it does not re-measure. The question it answers is "can the
README's numbers be derived from the data that was collected", NOT "what would
a run today produce".

Usage:
    python3 docs/internal/audit-reports/bench-probe-2026-09/analyze_probe.py

No network, no benchmark, no Go toolchain. Reads probe-run{1,2,3}.txt from its
own directory.

The three statistics that are here and NOT in the workflow's own per-job summary
-------------------------------------------------------------------------------
1. CROSS-DISPATCH. The workflow summarises one job. #1497's bimodality was
   observed ACROSS dispatches, so the medians of separate jobs have to be put
   side by side to be on the same axis as the thing under investigation.

2. THE SHAPE DISCRIMINATOR. `load_sum` can grow two ways and they mean opposite
   things: every iteration gets slower (a level shift), or a few iterations
   stall (an episode). Splitting `load_sum` at the round's own p50 separates
   them: `p50 x iters` is the level, `load_sum - p50 x iters` is everything
   above it. Whichever of the two tracks the round total is the one that moved.
   ⚠️ The split is at the round's OWN p50, so both parts move if the whole
   distribution moves; what distinguishes them is which one CORRELATES with the
   round total, not which one is bigger.

3. WHAT MECHANISM 1 WOULD HAVE TO DO. Instead of asking "did we catch an
   episode" (a run can always fail to), ask how large a write-path episode would
   have to be to produce #1497's magnitude, and compare it against the largest
   one actually seen. That is answerable from a run that caught nothing.
"""

import pathlib
import re
import statistics as st
import sys

HERE = pathlib.Path(__file__).resolve().parent
RUNS = ["probe-run1.txt", "probe-run2.txt", "probe-run3.txt"]

# #1497's three same-day dispatches read M/W = +30.38% / +4.85% / +3.98%. The
# swing to be explained is the gap between the modes, in percentage points.
TARGET_SWING_PP = 30.38 - 3.98

ROW = re.compile(r"^PROBEROW (.*)$")
ENV = re.compile(r"^PROBEENV (.*)$")
TAIL = re.compile(r"^PROBETAIL round=(\d+) rank=(\d+) iter=(\d+) w=(\d+) l=(\d+)$")


def load(path):
    """Return (measurement rows, env lines, dropped calibration count)."""
    rows, env, tails = [], [], {}
    for line in path.read_text().splitlines():
        m = ROW.match(line)
        if m:
            d = dict(kv.split("=", 1) for kv in m.group(1).split())
            rows.append({k: int(v) for k, v in d.items()})
            continue
        m = ENV.match(line)
        if m:
            env.append(m.group(1).strip())
            continue
        m = TAIL.match(line)
        if m:
            tails.setdefault(int(m.group(1)), []).append((int(m.group(4)), int(m.group(5))))
    # Same rule as the workflow's summary: `-benchtime=Nx` still runs the body
    # once at b.N==1 to calibrate, and that pass is the first after process
    # start (cold caches, first-touch faults). Pooling it into a spread reports
    # start-up as measurement noise.
    calib = [r for r in rows if r.get("bench_n") == 1]
    if calib and any(r.get("bench_n", 1) > 1 for r in rows):
        rows = [r for r in rows if r.get("bench_n", 1) > 1]
    return rows, list(dict.fromkeys(env)), len(calib), tails


def corr(x, y):
    mx, my = st.mean(x), st.mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    den = (sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y)) ** 0.5
    return num / den if den else float("nan")


def ms(x):
    return x / 1e6


def main():
    sessions = []
    for name in RUNS:
        path = HERE / name
        if not path.exists():
            print(f"::error::missing data file {path}")
            return 2
        rows, env, ncal, tails = load(path)
        if len(rows) < 2:
            print(f"::error::{name}: parsed {len(rows)} measurement rows, need >= 2")
            return 2
        if not env:
            print(f"::error::{name}: no PROBEENV record — cannot identify what was measured")
            return 2
        sessions.append(dict(name=name, rows=rows, env=env, ncal=ncal, tails=tails))

    print("=" * 74)
    print("IDENTITY OF THE THING MEASURED")
    print("=" * 74)
    for s in sessions:
        print(f"  {s['name']}")
        for e in s["env"]:
            print(f"      PROBEENV {e}")
    envs = {e for s in sessions for e in s["env"]}
    # Strip the b.N field, which legitimately differs between the calibration
    # invocation and the measurement one.
    shapes = {re.sub(r" b\.N=\d+", "", e) for e in envs}
    print(f"\n  distinct (goos, goarch, numcpu, go, iters) tuples across all runs: {len(shapes)}")
    for sh in sorted(shapes):
        print(f"      {sh}")
    if len(shapes) != 1:
        print("  ⚠️ the runs are NOT all the same shape — do not pool them below")

    print()
    print("=" * 74)
    print("PER DISPATCH")
    print("=" * 74)
    for s in sessions:
        rows = s["rows"]
        tot = [r["write_sum"] + r["load_sum"] for r in rows]
        w = [r["write_sum"] for r in rows]
        med = st.median(tot)
        worst = max(rows, key=lambda r: r["write_sum"] + r["load_sum"])
        excess = (worst["write_sum"] + worst["load_sum"]) - med
        dw = worst["write_sum"] - st.median(w)
        print(f"\n  {s['name']}  ({len(rows)} measurement rounds, {s['ncal']} calibration dropped)")
        print(f"      round total   median {ms(med):8.1f} ms   min {ms(min(tot)):8.1f}   "
              f"max {ms(max(tot)):8.1f}   spread {100 * (max(tot) - min(tot)) / med:6.2f}%")
        print(f"      write_sum     median {ms(st.median(w)):8.3f} ms  min {ms(min(w)):8.3f}   "
              f"max {ms(max(w)):8.3f}   share of all time {100 * sum(w) / sum(tot):.3f}%")
        print(f"      write tail    worst p99 over rounds {max(r['write_p99'] for r in rows) / 1000:8.1f} us   "
              f"worst single write {max(r['write_max'] for r in rows) / 1000:8.1f} us")
        print(f"      slowest round #{worst['round']}: {ms(excess):+.1f} ms vs median round "
              f"({100 * excess / med:+.2f}%); the write half contributes {ms(dw):+.3f} ms "
              f"= {100 * dw / excess:.1f}% of that excess")

    print()
    print("=" * 74)
    print("CROSS DISPATCH — the axis #1497 observed its bimodality on")
    print("=" * 74)
    meds = [st.median([r["write_sum"] + r["load_sum"] for r in s["rows"]]) for s in sessions]
    gm = st.median(meds)
    for s, m in zip(sessions, meds):
        print(f"  {s['name']:<18} median round total {ms(m):8.1f} ms   "
              f"{100 * (m - gm) / gm:+6.2f}% vs the middle dispatch")
    print(f"  spread of the three medians: {100 * (max(meds) - min(meds)) / gm:.2f}%")
    print(f"  ⛔ #1497's gap between modes, for comparison: {TARGET_SWING_PP:.2f} pp")

    allrows = [r for s in sessions for r in s["rows"]]
    tot = [r["write_sum"] + r["load_sum"] for r in allrows]
    base = [r["load_p50"] * r["iters"] for r in allrows]
    above = [r["load_sum"] - r["load_p50"] * r["iters"] for r in allrows]
    w = [r["write_sum"] for r in allrows]

    print()
    print("=" * 74)
    print("SHAPE OF THE ROUND-TO-ROUND VARIATION — level shift or episode?")
    print("=" * 74)
    print(f"{'dispatch':<18}{'n':>4}{'corr(total, p50xN)':>21}{'corr(total, above-p50)':>24}"
          f"{'corr(total, write_sum)':>24}")
    for s in sessions:
        rs = s["rows"]
        t = [r["write_sum"] + r["load_sum"] for r in rs]
        b = [r["load_p50"] * r["iters"] for r in rs]
        a = [r["load_sum"] - r["load_p50"] * r["iters"] for r in rs]
        ww = [r["write_sum"] for r in rs]
        print(f"{s['name']:<18}{len(rs):>4}{corr(t, b):>21.3f}{corr(t, a):>24.3f}{corr(t, ww):>24.3f}")
    print(f"{'POOLED':<18}{len(allrows):>4}{corr(tot, base):>21.3f}{corr(tot, above):>24.3f}"
          f"{corr(tot, w):>24.3f}")

    print(f"\n  standard deviation over the pooled {len(allrows)} rounds:")
    print(f"      round total           {ms(st.stdev(tot)):8.2f} ms   (range {ms(max(tot) - min(tot)):7.2f} ms)")
    print(f"      level  (p50 x iters)  {ms(st.stdev(base)):8.2f} ms   (range {ms(max(base) - min(base)):7.2f} ms)"
          f"  = {100 * st.stdev(base) / st.stdev(tot):5.1f}% of the round total's sd")
    print(f"      above-p50 mass        {ms(st.stdev(above)):8.2f} ms   (range {ms(max(above) - min(above)):7.2f} ms)"
          f"  = {100 * st.stdev(above) / st.stdev(tot):5.1f}%")
    print(f"      write_sum             {ms(st.stdev(w)):8.2f} ms   (range {ms(max(w) - min(w)):7.2f} ms)"
          f"  = {100 * st.stdev(w) / st.stdev(tot):5.1f}%")
    print("\n  ⚠️ correlation says which quantity moves WITH the round; the sd column says"
          "\n     how much it could move the round at all. A term needs both to be a cause.")

    print()
    print("=" * 74)
    print("WHAT MECHANISM 1 WOULD HAVE TO DO")
    print("=" * 74)
    med_round = st.median(tot)
    need = TARGET_SWING_PP / 100 * med_round
    biggest = max(w) - st.median(w)
    iters = allrows[0]["iters"]
    print(f"  A one-sided swing of {TARGET_SWING_PP:.2f}% on a {ms(med_round):.0f} ms round is "
          f"{ms(need):.0f} ms.")
    print(f"  Largest write-path excursion above the median write_sum, in all "
          f"{len(allrows)} rounds: {ms(biggest):.1f} ms.")
    print(f"  => short by a factor of {need / biggest:.0f}x")
    print(f"  Per iteration: the median round's write half averages "
          f"{st.median(w) / iters / 1000:.1f} us;")
    print(f"     to reach {ms(need):.0f} ms it would have to average "
          f"{need / iters / 1e6:.2f} ms across all {iters} iterations,")
    print(f"     i.e. {need / st.median(w):.0f}x its entire observed cost, sustained — not in a spike.")
    print("\n  ⛔ This bounds the episodes that WERE seen. It is not a proof that no larger"
          "\n     episode exists: the high mode itself was not reproduced in these runs"
          "\n     (see the cross-dispatch spread above).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
