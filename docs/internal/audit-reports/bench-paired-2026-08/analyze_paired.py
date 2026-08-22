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
# The archived snapshot's SHAPE is part of that contract too, not a convenience
# check. Measured, not assumed: duplicating one night turns the pinned 5%/2 rule
# from 1 fire into 3 — a copied row can manufacture the very tickets this dataset
# exists to predict. Checking the COUNT is not enough; a six-night set shifted by
# one day passes a count check and still makes the "LAST four nights" label lie.
# So the dates themselves are pinned. (CodeRabbit, PR #1498, round 2.)
EXPECTED_DATES = ("2026-08-16", "2026-08-17", "2026-08-18",
                  "2026-08-19", "2026-08-20", "2026-08-21")
# ⛔ The NAMES, not a count of 22. Caught in this file's own round-4 self-review:
# a first cut pinned only `len(keys) == 22`, which is the very shape I had just
# argued against for the dates. Measured: rename one benchmark in all six nights
# and the count guard passes, then the run dies at `StatisticsError` from inside
# `statistics.quantiles` — a stack trace, not a statement of which contract broke.
# Every section below indexes specific names (ATTRIBUTED, UNSTABLE, the §3 list),
# so the identity of the 22 is the contract; their cardinality is a consequence.
EXPECTED_BENCHES = frozenset({
    "BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000",
    "BenchmarkControlCanaryCPU",
    "BenchmarkControlCanarySleep",
    "BenchmarkDiffAndReload_Hierarchical_1000_Churn10pct",
    "BenchmarkDiffAndReload_Hierarchical_1000_NoChange",
    "BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged",
    "BenchmarkDiffAndReload_MixedMode_500flat_500hier_NoChange",
    "BenchmarkFullDirLoad_1000",
    "BenchmarkFullDirLoad_Hierarchical_1000",
    "BenchmarkFullDirLoad_MixedMode_100flat_900hier",
    "BenchmarkFullDirLoad_MixedMode_500flat_500hier",
    "BenchmarkIncrementalLoad_1000_NoChange",
    "BenchmarkIncrementalLoad_1000_NoChange_MtimeGuard",
    "BenchmarkIncrementalLoad_1000_OneFileChanged",
    "BenchmarkMergePartialConfigs_1000",
    "BenchmarkResolveSilentModes_1000",
    "BenchmarkScanDirFileHashes_1000",
    "BenchmarkScanDirFileHashes_1000_MtimeGuard",
    "BenchmarkScanDirHierarchical_1000",
    "BenchmarkScanDirHierarchical_MixedMode_500flat_500hier",
    "BenchmarkScanFromConfigSource_1000_InMemory",
    "BenchmarkSimulate_DeepChain_L4",
})

CANARY = {"BenchmarkControlCanaryCPU", "BenchmarkControlCanarySleep"}
# Attributed to a real implementation cost and ACCEPTED in #1474 — excluded when
# the question is "how noisy is a bench-night", included everywhere else.
ATTRIBUTED = {"BenchmarkMergePartialConfigs_1000", "BenchmarkResolveSilentModes_1000"}
# Measured run-to-run bimodal, #1497. Excluded only where the README says so.
UNSTABLE = "BenchmarkIncrementalLoad_1000_OneFileChanged"
# The window the README's `+27.94%` comes from — nights 3..6 of the six.
#
# ⛔ CORRECTION. This used to be a second hand-written date tuple, justified in
# a code comment, a review reply and a commit message with: "adding a seventh
# night makes the constant obviously stale". That was FALSE, and measured false:
# with a seventh night appended, 08-18..21 are all still present, so the
# incomplete-window branch never fires and the script keeps printing "LAST four
# nights" for what are no longer the last four — while also calling a seven-night
# figure "the six-night value". I rejected `v[-4:]` for silently re-pointing, then
# shipped something that silently MIS-LABELS. Same disease, different organ.
#
# Deriving it from EXPECTED_DATES is not the same mistake: that is a slice of a
# PINNED CONSTANT, not of the incoming data, so it cannot drift with the input —
# and it makes "the last four" true by construction rather than by assertion.
FOUR_NIGHT_WINDOW = EXPECTED_DATES[-4:]


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

    nights = d.get("nights")
    if not isinstance(nights, list) or not nights:
        raise ValueError(f"nights must be a non-empty list, got {type(nights).__name__} "
                         f"of length {len(nights) if isinstance(nights, list) else 'n/a'} "
                         "— an empty series would otherwise die deeper in with an "
                         "IndexError instead of naming the broken contract")
    if not all(isinstance(n, dict) for n in nights):
        raise ValueError("every night must be an object; refusing to analyse")

    dates = [n.get("night_utc") for n in nights]
    if not all(isinstance(x, str) for x in dates):
        raise ValueError(f"every night needs a string night_utc, got {dates!r}")
    # Checked separately from the identity test below even though it is implied
    # by it, because the two failures have different causes and the message has
    # to name the right one: a duplicated row is a corrupted snapshot, a
    # different date list is the wrong snapshot.
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        raise ValueError(f"night_utc must be unique and strictly increasing, got {dates!r} "
                         "— duplicated nights fabricate consecutive-night runs: one copied "
                         "row takes the pinned 5%/2 rule from 1 fire to 3")
    if tuple(dates) != EXPECTED_DATES:
        raise ValueError(f"this is a pinned archival snapshot of {list(EXPECTED_DATES)}, "
                         f"got {dates!r} — every figure below, and the "
                         f"{FOUR_NIGHT_WINDOW[0]}..{FOUR_NIGHT_WINDOW[-1]} window in "
                         "particular, is named for THESE nights; refusing to analyse")

    keysets = []
    for night, n in zip(dates, nights):
        ratios = n.get("ratios_pct")
        if not isinstance(ratios, dict):
            raise ValueError(f"night {night} has no ratios_pct mapping")
        keysets.append(frozenset(ratios))
    for night, keys in zip(dates, keysets):
        if keys != EXPECTED_BENCHES:
            missing = sorted(EXPECTED_BENCHES - keys)
            extra = sorted(keys - EXPECTED_BENCHES)
            raise ValueError(f"night {night} does not carry the pinned "
                             f"{len(EXPECTED_BENCHES)} benchmarks (missing {missing}, "
                             f"extra {extra}) — a ragged series silently changes the "
                             "spread table's n while every row still renders, and a "
                             "renamed one makes the sections below index a bench that "
                             "is not there; refusing to analyse")

    return list(zip(dates, (n["ratios_pct"] for n in nights)))


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
    #
    # No "window incomplete" fallback here any more. It used to exist and it was
    # WORSE than nothing: it read as a safety net while being unreachable in the
    # one case it advertised (a seventh night leaves 08-18..21 intact, so it never
    # fired and the wrong label printed anyway). `load()` now enforces the exact
    # date list, so this window is present by contract — an unreachable branch
    # that looks like a guard is the same failure mode this directory documents.
    four = [r[UNSTABLE] for night, r in nights if night in FOUR_NIGHT_WINDOW]
    print(f"  ⇒ same bench over the LAST four nights "
          f"({FOUR_NIGHT_WINDOW[0]}..{FOUR_NIGHT_WINDOW[-1]}, n={len(four)}): "
          f"thr={threshold(four):+7.2f}%  — the six-night value above is "
          f"{threshold([r[UNSTABLE] for _, r in nights]):+.2f}%; "
          f"that gap is the point")
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
