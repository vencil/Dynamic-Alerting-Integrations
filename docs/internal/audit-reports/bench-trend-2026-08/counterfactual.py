#!/usr/bin/env python3
"""step13 — post-fix counterfactual validation against the REAL 30-night series.

⚠️ PROVENANCE: the original step13 was not present in this session's scratchpad
(only `cf/{analyze_bench_history,_lib_compat,_lib_exitcodes}.py` survived), so
this is a reconstruction. Unlike `step14b_post_shrink.py` — which reimplemented
"no findings → close" and therefore measured a MODEL, not the code — this
harness drives the real module: it imports `analyze_bench_history` and calls
`analyze_trend` / `run_trend_watch` directly.

Data: the `{per_night_stats,nights_meta}.csv` sitting next to this file — 30 real
nightly `bench-record` runs (2026-07-15..2026-08-13), 20 benchmarks, per-night
median ns/op + the runner's `cpu:` string. See README.md for provenance and for
why that data is committed rather than regenerated on demand.

Three checks:
  1. the #1396 window (today = 2026-08-12) is INCONCLUSIVE and does NOT fire
  2. zero false-positive bench-nights across the 17 windows
  3. the detector is byte-identical between HEAD and the working tree — over
     every clean window AND over every +20% permanent-step injection scenario.
     This round touches the CLOSE path and the PROSE only; check 3 is what makes
     "the fire arithmetic is untouched" a measurement rather than an assertion.

⚠️ NOT a check, reported as diagnostics only: the δ=20% attributable detection
rate. CHANGELOG quotes 83.1% over 260 scenarios with a median 2-night delay;
that harness is gone and its scenario construction is not recoverable from the
number alone (this file's reconstruction yields 240 scenarios). The two are NOT
comparable and this file does not claim to reproduce 83.1%.

Usage:  python3 -B counterfactual.py [--tool PATH] [--skip-slow]
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERIES = HERE                                   # the CSVs live beside this file
REPO = HERE.parents[3]                          # docs/internal/audit-reports/<this>
DEFAULT_TOOL = REPO / "scripts/tools/dx/analyze_bench_history.py"

RECENT_K = 3
MIN_FLOOR = 5.0
CANARY_MULT = 3.0
CREEP_FLOOR = 10.0
WINDOW = 14
# Minimum nights of runway a scenario must have AFTER its injection night. It
# bounds which nights may be injection points (`starts` below) — it does NOT cap
# how long detection is watched for: each scenario is replayed to the end of the
# series. Capping the replay would shrink check 3's comparison surface, which is
# the opposite of what check 3 is for.
INJECT_MIN_RUNWAY = 5


def load_module(path: Path, name: str):
    sys.path.insert(0, str(path.parent))
    sys.path.insert(0, str(path.parent.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_series() -> list[dict]:
    """Oldest-first list of {night, cpu, medians{bench: ns}}."""
    cpu = {}
    with open(SERIES / "nights_meta.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            cpu[row["night_utc"]] = row["cpu_model"] or None
    per: dict[str, dict[str, float]] = {}
    with open(SERIES / "per_night_stats.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            per.setdefault(row["night_utc"], {})[row["bench"]] = float(row["median_ns"])
    return [{"night": n, "cpu": cpu.get(n), "medians": per[n]} for n in sorted(per)]


def to_records(ab, series: list[dict], today_idx: int, scale=None) -> list:
    """The newest-first `NightRecord` window whose newest night is `today_idx`."""
    lo = max(0, today_idx - WINDOW + 1)
    out = []
    for i in range(today_idx, lo - 1, -1):
        row = series[i]
        med = dict(row["medians"])
        if scale:
            bench, start, factor = scale
            if i >= start and bench in med:
                med[bench] *= factor
        rec = ab.NightRecord(run_id=1000 + i, created_at=f"{row['night']}T05:00:00Z",
                             medians=med)
        rec.cpu_model = row["cpu"]
        out.append(rec)
    return out


def analyse(ab, nights):
    return ab.analyze_trend(nights, RECENT_K, MIN_FLOOR, CANARY_MULT, CREEP_FLOOR)


def check1(ab, series) -> tuple[bool, str]:
    idx = [i for i, r in enumerate(series) if r["night"] == "2026-08-12"][0]
    findings, meta = analyse(ab, to_records(ab, series, idx))
    ok = meta["status"] == ab.STATUS_INCONCLUSIVE and not findings
    return ok, (f"today=2026-08-12  status={meta['status']}  findings={len(findings)}  "
                f"host={meta['today_cpu_model']}  same-class nights="
                f"{meta['n_class_nights']}/{meta['n_nights']}")


def check2(ab, series) -> tuple[bool, str]:
    evaluable = fp = 0
    for idx in range(WINDOW - 1, len(series)):
        findings, meta = analyse(ab, to_records(ab, series, idx))
        if meta["status"] != ab.STATUS_INCONCLUSIVE:
            evaluable += 1
        fp += len(findings)
    return fp == 0, (f"{len(series) - WINDOW + 1} windows, {evaluable} evaluable, "
                     f"false-positive bench-nights={fp}")


def injection_sweep(ab, series, delta=0.20):
    """Every (bench, injection-night) scenario, and what the detector says on
    every night from the injection onward. Returned as a signature so two
    builds can be compared exactly, plus the diagnostics tuple."""
    benches = sorted({b for r in series for b in r["medians"]})
    starts = list(range(WINDOW - 1, len(series) - INJECT_MIN_RUNWAY))
    sig = []
    detected, delays, scenarios = 0, [], 0
    for bench in benches:
        for start in starts:
            scenarios += 1
            hit = None
            for today in range(start, len(series)):
                findings, meta = analyse(
                    ab, to_records(ab, series, today, (bench, start, 1 + delta)))
                sig.append([bench, series[start]["night"], series[today]["night"],
                            meta["status"],
                            sorted([f.bench, f.kind] for f in findings)])
                if hit is None and any(f.bench == bench for f in findings):
                    hit = today - start
            if hit is not None:
                detected += 1
                delays.append(hit)
    med = statistics.median(delays) if delays else float("nan")
    return (json.dumps(sig, sort_keys=True),
            f"δ={delta:.0%}  scenarios={scenarios}  detected-by-series-end={detected} "
            f"({100.0 * detected / scenarios:.1f}%)  median delay={med:.0f} nights")


def replay_signature(ab, series) -> str:
    """Everything the DETECTOR decides, for every window, as one hashable blob."""
    out = []
    for idx in range(WINDOW - 1, len(series)):
        findings, meta = analyse(ab, to_records(ab, series, idx))
        out.append({
            "night": series[idx]["night"],
            "status": meta["status"],
            "stratification": meta["stratification"],
            "floor": round(meta["floor_pct"], 9),
            "creep_floor": round(meta["creep_floor_pct"], 9),
            "canary_cv": round(meta["canary_cv"], 12),
            "evaluated": meta["evaluated_benches"],
            "inconclusive": meta["inconclusive_benches"],
            "reasons": meta["inconclusive_reasons"],
            "findings": sorted([f.bench, f.kind, round(f.pct_vs_anchor, 9),
                                round(f.pct_typical_vs_anchor, 9)] for f in findings),
        })
    return json.dumps(out, sort_keys=True)


def check3(ab, series) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as td:
        head = Path(td) / "abh_head.py"
        head.write_bytes(subprocess.run(
            ["git", "show", "HEAD:scripts/tools/dx/analyze_bench_history.py"],
            cwd=REPO, capture_output=True, check=True).stdout)
        ab_head = load_module(head, "abh_head")
        clean_head = replay_signature(ab_head, series)
        inj_head, _diag_head = injection_sweep(ab_head, series)
    clean_now = replay_signature(ab, series)
    inj_now, diag = injection_sweep(ab, series)
    ok = clean_head == clean_now and inj_head == inj_now
    return ok, (f"clean replay ({len(series) - WINDOW + 1} windows): "
                f"{'IDENTICAL' if clean_head == clean_now else 'DIFFERS'}; "
                f"+20% injection sweep: "
                f"{'IDENTICAL' if inj_head == inj_now else 'DIFFERS'}\n"
                f"        diagnostics (NOT compared to CHANGELOG's 83.1%): {diag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", type=Path, default=DEFAULT_TOOL)
    ap.add_argument("--skip-slow", action="store_true")
    args = ap.parse_args()
    ab = load_module(args.tool, "abh_wt")
    series = load_series()
    print(f"series: {len(series)} nights {series[0]['night']}..{series[-1]['night']}, "
          f"{len({b for r in series for b in r['medians']})} benches\n")
    results = []
    for name, fn in (("1  #1396 window does not fire", lambda: check1(ab, series)),
                     ("2  zero false positives", lambda: check2(ab, series)),
                     ("3  detector unchanged vs HEAD (clean + injected)",
                      lambda: check3(ab, series))):
        if args.skip_slow and name.startswith("3"):
            continue
        ok, detail = fn()
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    print(f"\n{sum(results)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
