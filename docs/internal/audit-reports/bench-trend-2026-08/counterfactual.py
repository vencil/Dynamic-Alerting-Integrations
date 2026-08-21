#!/usr/bin/env python3
"""step13 — post-fix counterfactual validation against the REAL 30-night series.

⚠️ PROVENANCE: the original step13 was not present in this session's scratchpad
(only `cf/{analyze_bench_history,_lib_compat,_lib_exitcodes}.py` survived), so
this is a reconstruction. Unlike `step14b_post_shrink.py` — which reimplemented
"no findings → close" and therefore measured a MODEL, not the code — this
harness drives the real module: it imports `analyze_bench_history` and calls
`analyze_trend` / `run_trend_watch` directly.

⛔ THAT SENTENCE WAS FALSE UNTIL CHECK 4 EXISTED, and the correction is left
visible rather than quietly patched. `run_trend_watch` appeared nowhere in this
file except the line above: checks 1-3 all call `analyze_trend`, which is the
DETECTOR. Nothing here drove the open/update/close path. That is the same class
of overclaim `bench-workload-effect.yaml` documents about itself — "a check that
is described but not implemented is worse than no check", because the reader
assumes something already guards it. Check 4 makes the sentence true.

Data: the `{per_night_stats,nights_meta}.csv` sitting next to this file — 30 real
nightly `bench-record` runs (2026-07-15..2026-08-13), 20 benchmarks, per-night
median ns/op + the runner's `cpu:` string. See README.md for provenance and for
why that data is committed rather than regenerated on demand.

Four checks:
  1. the #1396 window (today = 2026-08-12) is INCONCLUSIVE and does NOT fire
  2. zero false-positive bench-nights across the 17 windows
  3. the detector is byte-identical between HEAD and the working tree — over
     every clean window AND over every +20% permanent-step injection scenario.
     This round touches the CLOSE path and the PROSE only; check 3 is what makes
     "the fire arithmetic is untouched" a measurement rather than an assertion.
  4. the CLOSE path, measured: `run_trend_watch` driven night after night with
     each night's rendered body fed back as the next night's issue body — the
     marker is the tool's only cross-night memory (see
     `analyze_bench_history.py` §"HOW OFTEN THAT HAPPENS IS UNMEASURED"), so a
     harness that skips the feedback is not testing this code. Reports how often
     a permanent regression's own ticket gets auto-closed while the regression
     is still there.

⚠️ NOT a check, reported as diagnostics only: the δ=20% attributable detection
rate. CHANGELOG quotes 83.1% over 260 scenarios with a median 2-night delay;
that harness is gone and its scenario construction is not recoverable from the
number alone (this file's reconstruction yields 240 scenarios). The two are NOT
comparable and this file does not claim to reproduce 83.1%.

Usage:  python3 -B counterfactual.py [--tool PATH] [--skip-slow]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import importlib.util
import io
import json
import statistics
import subprocess
import sys
import tempfile
import types
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

# ── check 4 (close path) ────────────────────────────────────────────────────
# A scenario only counts toward the mis-close rate if the replay ran long enough
# AFTER the ticket opened to see a close happen. Without that filter the rate is
# diluted by scenarios where the series simply ended — "not closed yet" read as
# "safe", which is the misreading ADR-032 explicitly warns is not citable.
MISCLOSE_OBSERVABLE_RUNWAY = 10
# (mis-closed, scenarios with that much runway), measured by this check. It is a
# RATCHET, not a target: more mis-closes than this fails, fewer passes and says
# so loudly so the number is tightened deliberately instead of drifting. The
# figure ADR-032 quotes is 120/120; this reproduces it FROM THE CODE — the
# scratchpad harness that originally produced it is gone.
#
# ⛔ WHILE THE BASELINE IS 100% THE RATCHET HAS NO TEETH, and saying so is the
# point: at 120/120 there is no "worse" for it to catch, so today check 4 can
# only fail on a broken harness (self-check), a close landing outside the runway
# filter, or the scenario population changing. It gets teeth the moment the
# close path is fixed and this drops — which is exactly when a silent regression
# back to auto-closing would matter. Do not read a PASS today as "the close path
# is guarded".
MISCLOSE_BASELINE = (120, 120)
# Issue number handed to the tool in fixture mode. Any int works; it only has to
# stay the same across a scenario's nights so the replay looks like one ticket.
REPLAY_ISSUE = 4242

# What `run_trend_watch` prints on each branch, in dry-run. Verified unique in
# the tool: "would open" has one site and "would close" one; "would update" has
# three and "NOT closing" two, but every site of each means the branch it is
# mapped to here. Matching stderr is the idiom the unit tests already use.
_ACT_OPEN = "would open new perf-trend issue"
_ACT_CLOSE = "would close"
_ACT_HOLD = "NOT closing perf-trend issue"
_ACT_UPDATE = "would update"


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


class ReplayError(RuntimeError):
    """A replay night that could not be interpreted. Never swallowed: see `replay`."""


def _fixture_from_records(recs, path: Path) -> Path:
    """Serialise a `to_records()` window into the fixture `run_trend_watch` reads.

    Built from `to_records` rather than from the CSVs directly so the injection
    arithmetic is literally the same code check 3 exercises — two spellings of
    "+20% from night N" would eventually disagree.
    """
    data = [{"run_id": r.run_id, "createdAt": r.created_at, "benches": r.medians,
             **({"cpu_model": r.cpu_model} if r.cpu_model else {})}
            for r in recs]
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def replay(ab, series, bench, start, delta, fixture: Path) -> list[str | None]:
    """Drive `run_trend_watch` from `start` to the end of the series.

    The issue body is the tool's ONLY cross-night memory, so each night's
    rendered body is fed in as the next night's issue body.

    ⚠️ MEASURED, and it contradicts the obvious justification for doing that:
    removing the feed-forward does NOT move this check's number (120/120 either
    way). On every close night in this sweep the injected bench IS in
    `evaluated_benches` and `inconclusive_benches` is empty, so the per-bench
    close guard's `unverified` set is empty and the marker never gets a vote —
    these mis-closes come from the SLIDING ANCHOR, not from lost state. The
    feed-forward stays because it is what makes the replay faithful, and because
    ADR-032's second stage puts per-bench state (the ACCEPTED exit) in that
    marker, where it will matter. It is NOT what makes today's figure valid, and
    must not be cited as though it were.

    `render_trend_issue_body` is spied on rather than parsed out of stdout
    because only the FINDINGS branch prints its body; the held-open and
    INCONCLUSIVE refreshes render one without printing it.
    """
    open_issue, open_body, acts = None, None, []
    real_render = ab.render_trend_issue_body
    cap: dict[str, str] = {}

    def spy(findings, meta, state=None, tracked=None):
        body = real_render(findings, meta, state, tracked)
        cap["body"] = body
        return body

    lo = start if start is not None else WINDOW - 1
    for today in range(lo, len(series)):
        cap.clear()
        scale = (bench, start, 1 + delta) if bench is not None else None
        _fixture_from_records(to_records(ab, series, today, scale), fixture)
        args = types.SimpleNamespace(
            fixture_json=fixture, fixture_open_issue=open_issue,
            fixture_open_body=open_body, fixture_open_labels=None, cache_dir=None,
            workflow="bench-record.yaml", trend_limit=WINDOW, recent_nights=RECENT_K,
            min_floor_pct=MIN_FLOOR, canary_floor_mult=CANARY_MULT,
            creep_floor_pct=CREEP_FLOOR, assignee="vencil", dry_run=True)
        err = io.StringIO()
        had_open = open_issue is not None
        ab.render_trend_issue_body = spy
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(err):
                rc = ab.run_trend_watch(args)
        finally:
            ab.render_trend_issue_body = real_render
        e = err.getvalue()
        # ⛔ Both of these used to be swallowed, and both fail in the direction
        # that UNDER-counts mis-closes: a night that errored out, or one whose
        # output matched no action marker, simply recorded nothing — so a broken
        # replay reads as a close path that stopped mis-closing. Louder is the
        # only safe direction for a harness whose headline number is a defect
        # rate. (CodeRabbit, PR #1496.)
        if rc != 0:
            raise ReplayError(f"run_trend_watch returned rc={rc} on "
                              f"{series[today]['night']}: {e.strip()[:300]}")
        if _ACT_OPEN in e:
            act, open_issue, open_body = "open", REPLAY_ISSUE, cap.get("body")
        elif _ACT_CLOSE in e:
            act, open_issue, open_body = "close", None, None
        elif _ACT_HOLD in e:
            act, open_body = "hold", cap.get("body", open_body)
        elif _ACT_UPDATE in e:
            act, open_body = "update", cap.get("body", open_body)
        else:
            act = None
        if had_open and act is None:
            raise ReplayError(f"an issue was open on {series[today]['night']} and the "
                              f"night matched no action marker: {e.strip()[:300]}")
        acts.append(act)
    return acts


def _sweep(ab, series, benches, starts):
    """Clean replay + one replay per (bench, injection night). Raises `ReplayError`."""
    with tempfile.TemporaryDirectory() as td:
        fx = Path(td) / "nights.json"
        clean = replay(ab, series, None, None, 0.0, fx)
        rows = []
        for bench in benches:
            for start in starts:
                acts = replay(ab, series, bench, start, 0.20, fx)
                o = next((i for i, a in enumerate(acts) if a == "open"), None)
                c = (next((i for i, a in enumerate(acts) if a == "close" and i > o), None)
                     if o is not None else None)
                rows.append((o, c, None if o is None else len(acts) - o - 1,
                             sum(1 for a in acts if a == "hold")))
    return clean, rows


def check4(ab, series) -> tuple[bool, str]:
    """The close path, measured: does a permanent regression's ticket auto-close?"""
    starts = list(range(WINDOW - 1, len(series) - INJECT_MIN_RUNWAY))
    benches = sorted({b for r in series for b in r["medians"]})
    try:
        clean, rows = _sweep(ab, series, benches, starts)
    except ReplayError as exc:
        # Reported as a FAILED check rather than a traceback: the run still has
        # a verdict line, and the verdict is "this measurement did not happen".
        return False, f"replay aborted — the number below does not exist: {exc}"

    opened = [r for r in rows if r[0] is not None]
    # ⛔ SELF-CHECK, both directions. A harness that never opens a ticket reports
    # zero mis-closes — a broken harness and a fixed close path produce the same
    # number. So: the clean series must produce NO ticket, and the injected sweep
    # must produce SOME. Neither alone is enough.
    self_ok = not any(clean) and bool(opened)
    delays = [r[1] - r[0] for r in opened if r[1] is not None]
    # If a close can land beyond the runway filter, the filter is selecting the
    # wrong population and the rate below is not the rate it claims to be.
    runway_ok = not delays or max(delays) < MISCLOSE_OBSERVABLE_RUNWAY
    observable = [r for r in opened if r[2] >= MISCLOSE_OBSERVABLE_RUNWAY]
    mis = [r for r in observable if r[1] is not None]
    base_mis, base_n = MISCLOSE_BASELINE
    ok = (self_ok and runway_ok and len(observable) == base_n
          and len(mis) <= base_mis)
    note = ""
    if self_ok and runway_ok and len(observable) == base_n and len(mis) < base_mis:
        note = (f"\n        ⬇ better than the recorded baseline ({base_mis}/{base_n}) "
                f"— tighten MISCLOSE_BASELINE deliberately, do not let it drift")
    open_delay = statistics.median([r[0] for r in opened]) if opened else float("nan")
    return ok, (
        f"{len(rows)} scenarios ({len(benches)} benches x {len(starts)} injection "
        f"nights, δ=+20% permanent); opened={len(opened)} "
        f"(median {open_delay:.0f} night(s) after injection)\n"
        f"        MIS-CLOSED while still regressed: {len(mis)}/{len(observable)} of the "
        f"scenarios with >={MISCLOSE_OBSERVABLE_RUNWAY} nights of runway after the open "
        f"(baseline {base_mis}/{base_n})\n"
        f"        open->close: median {statistics.median(delays):.0f}, max {max(delays)} "
        f"nights (n={len(delays)}); held-open nights seen: {sum(r[3] for r in rows)}\n"
        f"        self-check: clean series opens nothing = {not any(clean)}; injected "
        f"sweep opens something = {bool(opened)}; runway covers the longest close = "
        f"{runway_ok}{note}"
        if delays else
        f"{len(rows)} scenarios; opened={len(opened)}; NO close observed — "
        f"self-check clean={not any(clean)} opened={bool(opened)}")


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
                      lambda: check3(ab, series)),
                     ("4  close path: permanent regression's ticket auto-closes",
                      lambda: check4(ab, series))):
        if args.skip_slow and name.startswith("3"):
            continue
        ok, detail = fn()
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    print(f"\n{sum(results)}/{len(results)} PASS")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
