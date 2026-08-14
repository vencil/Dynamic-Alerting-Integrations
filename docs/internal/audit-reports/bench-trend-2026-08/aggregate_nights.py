#!/usr/bin/env python3
"""Per-night median/mean per benchmark, built ONLY from parsed raw log rows.

Step 2 of the reconstruction: consumes the `raw_samples.json` written by
`parse_logs.py` and emits `per_night_stats.csv` — one of the two CSVs that
`counterfactual.py` reads.

⚠️ NOT RUNNABLE AS-IS, same as `parse_logs.py`: its inputs (`raw_samples.json`,
`nights.json`) are intermediates that are NOT committed. Kept as the record of
how `per_night_stats.csv` was derived. See README.md.

Usage (only with regenerated intermediates):  python3 aggregate_nights.py OUT_DIR
"""
import json, csv, sys, statistics as st, collections

if len(sys.argv) != 2:
    sys.exit(__doc__)
OUT = sys.argv[1]
samples = json.load(open(f"{OUT}/raw_samples.json"))
nights = json.load(open(f"{OUT}/nights.json"))

by = collections.defaultdict(list)
for s in samples:
    by[(s["night_utc"], s["bench"])].append(s)

rows = []
for (night, bench), ss in by.items():
    ss.sort(key=lambda x: x["sample_index"])
    v = [x["ns_op"] for x in ss]
    # steady-state view: drop sample 1 (warm-up), matching the Makefile's
    # "samples 2..N treated as steady-state by Phase 2 median-of-5" comment
    v_ss = v[1:] if len(v) > 1 else v
    rows.append(dict(
        night_utc=night, bench=bench, run_id=ss[0]["run_id"],
        run_number=ss[0]["run_number"], head_sha=ss[0]["head_sha"],
        n_samples=len(v), complete=(len(v) == 6),
        median_ns=st.median(v), mean_ns=st.mean(v),
        min_ns=min(v), max_ns=max(v),
        stdev_ns=st.stdev(v) if len(v) > 1 else 0.0,
        cv_pct=(st.stdev(v) / st.mean(v) * 100) if len(v) > 1 else 0.0,
        median_ns_steady=st.median(v_ss), mean_ns_steady=st.mean(v_ss),
        samples_ns=";".join(f"{x:.0f}" for x in v),
    ))

rows.sort(key=lambda r: (r["bench"], r["night_utc"]))
with open(f"{OUT}/per_night_stats.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader()
    for r in rows:
        w.writerow(r)
json.dump(rows, open(f"{OUT}/per_night_stats.json", "w"), indent=1)
print("wrote per_night_stats.csv/json:", len(rows), "series rows")
