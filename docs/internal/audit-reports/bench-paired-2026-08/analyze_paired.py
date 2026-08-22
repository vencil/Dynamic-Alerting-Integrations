#!/usr/bin/env python3
"""Read `nights.json` and re-derive every number this directory's README quotes.

Unlike `bench-trend-2026-08/`'s reconstruction scripts — which are kept for
auditability but cannot be re-run — this one IS runnable: its only input is the
committed `nights.json` sitting next to it.

    python3 -B analyze_paired.py

⛔ THE UNIT IS A RATIO, NOT ns/op. `bench-trend-2026-08/per_night_stats.csv`
holds absolute medians; this file holds `(main / pinned reference - 1) * 100`
measured on ONE runner per night. The two are not interchangeable and must never
be pooled: the whole point of ADR-032 is that the machine term cancels in the
ratio and does not cancel in the absolute series.
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The dataset's contract. Checked on load, because every number below changes
# meaning if any of the three moves and NOTHING about the output would look
# different: swap the unit for absolute ns/op and the "spread" table still
# prints, swap the pinned reference and the ratios still look like ratios. A
# wrong number that renders correctly is the failure mode this whole directory
# exists to document. (CodeRabbit, PR #1498.)
EXPECTED_SCHEMA = "bench-paired-series/v1"
EXPECTED_UNIT = "percent change of main vs the pinned reference (M/R - 1) * 100"
EXPECTED_REFERENCE_SHA = "3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0"

CANARY = {"BenchmarkControlCanaryCPU", "BenchmarkControlCanarySleep"}
# Attributed to a real implementation cost and ACCEPTED in #1474 — excluded when
# the question is "how noisy is a bench-night", included everywhere else.
ATTRIBUTED = {"BenchmarkMergePartialConfigs_1000", "BenchmarkResolveSilentModes_1000"}
# Measured run-to-run bimodal, #1497. Excluded only where the README says so.
UNSTABLE = "BenchmarkIncrementalLoad_1000_OneFileChanged"
# The window the README's `+27.94%` comes from — nights 3..6 of the six, i.e.
# the LAST four. Named by date rather than sliced off the end, so adding a
# seventh night makes the constant obviously stale instead of silently
# re-pointing at a different four nights.
FOUR_NIGHT_WINDOW = ("2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21")


def load():
    d = json.loads((HERE / "nights.json").read_text(encoding="utf-8"))
    if d.get("schema") != EXPECTED_SCHEMA:
        raise ValueError(f"unsupported schema {d.get('schema')!r} "
                         f"(expected {EXPECTED_SCHEMA!r}) — refusing to analyse")
    if d.get("unit") != EXPECTED_UNIT:
        raise ValueError(f"unexpected unit {d.get('unit')!r} — every statistic below "
                         "assumes a percent ratio; refusing to analyse")
    got = (d.get("reference") or {}).get("sha")
    if got != EXPECTED_REFERENCE_SHA:
        raise ValueError(f"pinned reference is {got!r}, expected "
                         f"{EXPECTED_REFERENCE_SHA!r} — these ratios would be measured "
                         "against a different baseline; refusing to analyse")
    return [(n["night_utc"], n["ratios_pct"]) for n in d["nights"]]


def fires(nights, benches, thr: float, k: int) -> dict[str, str]:
    """First night on which a bench has been over `thr` for `k` nights running.

    This is the pinned second-stage rule (ADR-032 §待決 5: fixed threshold,
    K consecutive nights) replayed over the real series — NOT a model of it.
    A night with no reading for that bench breaks the run rather than being
    skipped: "not measured" is not "under the threshold".
    """
    out = {}
    for b in benches:
        run = 0
        for night, ratios in nights:
            v = ratios.get(b)
            if v is None:
                run = 0
                continue
            run = run + 1 if v > thr else 0
            if run >= k:
                out[b] = night
                break
    return out


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(int(round(q / 100 * (len(vals) - 1))), len(vals) - 1)]


def spread(vals, label):
    print(f"  {label:<34} n={len(vals):>3}  median={st.median(vals):5.2f}%  "
          f"p90={pct(vals, 90):5.2f}%  max={max(vals):6.2f}%  "
          f">5%: {sum(1 for x in vals if x > 5):>2}  >3%: {sum(1 for x in vals if x > 3):>2}")


def q13(v):
    q = st.quantiles(sorted(v), n=4, method="inclusive")
    return q[0], q[2]


def threshold(v):
    """rustc-perf's rule, verbatim: significant iff `result > Q3 + IQR * 3`.

    Source read directly: rust-lang/rustc-perf `docs/comparison-analysis.md`.
    Applied here to see what it WOULD do on this series — see the README for why
    the answer is "do not adopt it in this form".
    """
    q1, q3 = q13(v)
    return q3 + 3 * (q3 - q1)


def main() -> int:
    nights = load()
    benches = sorted({b for _, r in nights for b in r} - CANARY)
    print(f"series: {len(nights)} nights {nights[0][0]}..{nights[-1][0]}, "
          f"{len(benches)} benchmarks + {len(CANARY)} control\n")

    print("=== 1. the pinned rule (5% / 2 consecutive nights) replayed ===")
    for thr in (5, 3, 2, 1):
        for k in (2, 3):
            f = fires(nights, benches, thr, k)
            names = ", ".join(f"{b.replace('Benchmark', '')}@{d[5:]}"
                              for b, d in sorted(f.items())) or "none"
            print(f"  threshold {thr}% / {k} nights -> {len(f)} fire(s): {names}")

    print("\n=== 2. spread of a bench-night ===")
    allv = [abs(r[b]) for _, r in nights for b in r if b not in CANARY]
    spread(allv, "all benchmarks")
    spread([abs(r[b]) for _, r in nights for b in r
            if b not in CANARY and b not in ATTRIBUTED], "minus the 2 attributed (#1474)")
    spread([abs(r[b]) for _, r in nights for b in r
            if b not in CANARY and b not in ATTRIBUTED and b != UNSTABLE],
           "minus those and #1497's bench")

    print("\n=== 3. per-bench threshold (Q3 + 3*IQR) — feasibility, not a proposal ===")
    for b in (UNSTABLE, "BenchmarkMergePartialConfigs_1000", "BenchmarkFullDirLoad_1000"):
        v = [r[b] for _, r in nights if b in r]
        base = threshold(v)
        loo = [threshold([x for j, x in enumerate(v) if j != i]) for i in range(len(v))]
        print(f"  {b.replace('Benchmark', ''):<40} n={len(v)} thr={base:+7.2f}%  "
              f"leave-one-out {min(loo):+7.2f}% .. {max(loo):+7.2f}%  "
              f"(swing {max(loo) - min(loo):.2f} pts)")
    # ⛔ The README quotes BOTH windows, because "the threshold itself moved 20
    # points when n went 4 -> 6" is the actual evidence that this rule needs more
    # history. Printing only one of them left half the argument unreproducible
    # while the module docstring promised otherwise. (CodeRabbit, PR #1498.)
    four = [r[UNSTABLE] for night, r in nights
            if night in FOUR_NIGHT_WINDOW and UNSTABLE in r]
    if len(four) == len(FOUR_NIGHT_WINDOW):
        print(f"  ⇒ same bench over the LAST four nights "
              f"({FOUR_NIGHT_WINDOW[0]}..{FOUR_NIGHT_WINDOW[-1]}, n={len(four)}): "
              f"thr={threshold(four):+7.2f}%  — the six-night value above is "
              f"{threshold([r[UNSTABLE] for _, r in nights if UNSTABLE in r]):+.2f}%; "
              f"that gap is the point")
    else:
        print(f"  ⚠️ four-night window incomplete ({len(four)}/{len(FOUR_NIGHT_WINDOW)} "
              f"nights present) — the README's four-night figure is NOT reproduced here")
    print("  ⛔ and the structural objection, shown rather than asserted:")
    for b in sorted(ATTRIBUTED):
        v = [r[b] for _, r in nights if b in r]
        print(f"     {b.replace('Benchmark', ''):<37} median {st.median(v):+.2f}%  "
              f"-> threshold {threshold(v):+.2f}%  (the threshold sits ABOVE the level, "
              f"so the accepted cost is absorbed)")

    print("\n=== 4. control canary vs the night's worst deviation ===")
    for night, r in nights:
        c = "/".join(f"{r[b]:+.2f}" for b in sorted(CANARY) if b in r)
        worst_v, worst_b = max(((abs(v), b) for b, v in r.items() if b not in CANARY),
                               default=(0.0, "-"))
        print(f"  {night}  canary {c:>14}   worst {worst_v:6.2f}%  "
              f"{worst_b.replace('Benchmark', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
