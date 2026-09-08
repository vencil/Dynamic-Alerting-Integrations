#!/usr/bin/env python3
"""TRK-374 — counterfactual validation of the level/above-p50 discriminant.

`README.md` §三 prints three quantities per component — `corr(round total, ·)`,
its own sd, and that sd as a share of the round total's sd — and asserts that
the last column is not decoration: correlation answers "which one moves with the
round", the share answers "how much could it move the round at all", and a
component needs both to be a cause. §四 recorded that as a DESIGN REASON with no
evidence behind it: the harness that had measured it was never committed and no
longer exists, so the numbers it produced were deleted rather than left
unverifiable (#1716 round 6). This file is what puts evidence back.

⛔ It drives the REAL module. `analyze_probe.load` parses the synthetic log,
`analyze_probe.reject` gets to refuse it, and the numbers checked below are
scraped out of what `render_ci` / `render_archive` actually PRINT. Nothing here
recomputes a correlation. The sibling precedent
`../bench-trend-2026-08/counterfactual.py` documents why: a harness that
reimplements the logic measures a MODEL, not the code.

⚠️ What IS reimplemented here, unavoidably, is the DATA PRODUCER — the row
fields the Go probe emits (`probeQ(sorted, q) = sorted[int(q*(len-1))]`, no
interpolation; max = last; sum). See `probeReport` in
`components/threshold-exporter/app/probe_write_latency_test.go`. That is the
thing under measurement's INPUT, not the thing under measurement.

⛔ THE OLD NUMBERS ARE NOT REPRODUCED AND THIS FILE DOES NOT TRY. The
parameters behind them are not recoverable from the numbers alone. The
synthetic shapes below were chosen for being cleanly constructible and are
printed in full at the top of every run — they are NOT reverse-engineered to
land on the old figures. Where the two disagree, the number this file prints is
the one that has a command behind it.

Checks, in the order they run:
  1. the tool exposes what this harness drives, and its identity is printed
  2. pure level shift: the shares match what the split's algebra predicts
  3. pure episode: `level` correlates at ~+1.000 and still owns a small share
  4. neither column alone separates the two shapes; together they do
     ⚠️ a DERIVATION from 2's and 3's measurements, not an independent
     constraint — given the tolerances it cannot fail while they pass;
     check 5 prints that subsumption rather than this line asserting it
  7. write-side variation: `total()` keeps both halves of the round
  5. mutation sensitivity — each of checks 2-4 and 7 is killed by >=1 broken
     discriminant, so "these checks have detection power" is measured
     here, and a check already red before any mutant is reported as NOT
     ASSESSABLE rather than credited with a vacuous kill
  6. on every shape the two renderers agree, each one's sd column matches its
     own share, and the round total's sd AND median both match the generated
     rows in absolute terms (ratios alone cannot see a shared unit bug; sd and
     corr alone cannot see an additive one)

⚠️ KNOWN BLIND SPOT, not fixed: every synthetic shape here is monotonic in the
round total, so a `corr()` swapped for another monotonic statistic (Spearman,
say) passes all seven. Measured by blind review. Closing it needs a
non-monotonic shape; a rank correlation is not a plausible regression, so the
gap is disclosed rather than papered over.

Exit codes: 0 all checks pass, 1 a check failed, 3 could not measure, and
argparse's own 2 for a usage error, before any measurement is attempted.

Usage:  python3 -B counterfactual.py [--tool PATH] [--dump SHAPE]
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import random
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]                      # docs/internal/audit-reports/<this>
DEFAULT_TOOL = REPO / "scripts/tools/dx/analyze_probe.py"

# ── synthetic construction parameters ────────────────────────────────────────
# ⛔ Every one of these is printed at the top of a run. A number whose parameters
# are not beside it is how §四 got into the state this file exists to fix.
SEED = 20260907
ROUNDS = 30                 # matches the workflow's rounds=30
ITERS = 400                 # matches the workflow's iters=400
BENCH_N = 30                # measurement rows; one bench_n=1 row is also emitted

# The per-iteration load profile is a BULK and a well-separated TAIL. Separated
# so a level shift applied to the bulk cannot reorder past the tail — the Go
# probe sorts, and a shift that reorders is no longer a shift of known shape.
# Magnitudes are shaped after the real archive (load_p50 ~7.06 ms, tail to
# ~22.8 ms); they are NOT a replica of it, and the resulting quantiles are
# printed so the reader can see how far off they are.
BULK_N = 300
BULK_LO_NS, BULK_HI_NS = 6_400_000, 7_600_000
TAIL_LO_NS, TAIL_HI_NS = 15_000_000, 22_800_000

LEVEL_SPAN_NS = 1_900_000   # shape A: per-round shift drawn from [0, span)
EPISODE_NS = 30_000_000     # shape B: one stalled iteration costs this much extra
EPISODE_MAX = 40            # shape B: per-round stall count drawn from [0, max]

WRITE_NS = 80_000           # per-iteration write cost, flat within a round
WRITE_JITTER_NS = 30_000    # per-ROUND, independent of load — see below

# ⛔ Shape "write" exists because of `total()`. In the two shapes above the write
# half is only noise, so a `total()` that returns `load_sum` alone — dropping the
# write half of the very quantity every correlation in the table is measured
# against — passed all six checks, with numbers CLOSER to the predictions than
# the correct implementation's (removing write also removes the only noise in
# total's variance). Found by blind review, reproduced against a real copy of
# the tool on disk, not reasoned about. This shape puts the variation where the
# other two do not, so dropping either half of `total` becomes visible.
# ⛔ The variation is a write-side EPISODE, not a write-side level shift. With a
# flat per-round write cost every write quantile equals the mean, so
# `write_p50 * iters == write_sum` EXACTLY and a `total()` built on the wrong
# one of those two was invisible — blind review found it by writing exactly that
# mutant. Stalling a few writes moves `write_sum` while leaving `write_p50`
# where it was, so the two stop being interchangeable. (It is also the shape
# mechanism 1 actually predicts.)
WRITE_SHAPE_LOAD_SPAN_NS = 10_000     # bulk jitter: keeps level/above non-degenerate
WRITE_EPISODE_NS = 2_000_000          # one stalled write costs this much extra
WRITE_EPISODE_MAX = 20                # per-round stalled-write count, drawn from [0, max]

# ⛔ WRITE_JITTER_NS exists so `write_sum` has a DEFINED correlation. With write
# constant across rounds its sd is 0 and the tool prints `+nan` there, which
# tells the reader nothing about the discriminant. It is independent of the load
# shape by construction, so it also serves as the negative control: a component
# that neither tracks nor could move the round has to come out looking like one.
# It costs the predictions below a little; `_predicted_shares` says why and every
# run prints prediction beside measurement. ⛔ No figure here — this is the third
# time a hand-typed number for that gap has gone stale.

TOL_PP = 2.0                # tolerance on a predicted-vs-measured share, in pp
TOL_CORR = 0.01             # |corr| must be within this of 1.0 where predicted
# check 7 has no closed-form prediction to compare against (the two halves are
# independent, so the round total's sd is a quadrature sum): it asserts the
# write component owns essentially all of the spread and the load components own
# essentially none. Both bounds are round numbers, not fitted to the output.
WRITE_SHARE_TOL_PP = 10.0
QUIET_SHARE_PP = 25.0
# "quiet" for a component that should not look like a cause of THIS shape's
# variation. A round number, well clear of every measured value — the assertion
# is "not ~+1", not a tight bound.
QUIET_CORR = 0.5
# check 6's sd-vs-share identity is computed from numbers already rounded for
# printing (sd to 2 dp in ms, share to 1 dp), so it cannot be exact.
SD_SHARE_IDENTITY_TOL_PP = 0.2
# The absolute sd anchor is compared against a number printed to 2 dp; the
# fraction is what absorbs that rounding, not a licence for the value to drift.
SD_ABS_TOL_FRAC = 0.001

# `render_ci`'s three table rows bind a label to a local; this maps that local to
# the role the checks reason about. ⛔ The pairs themselves are read out of the
# tool's AST, not typed here — a renamed local fails loudly instead of silently
# mis-attributing a row.
VAR_ROLE = {"lvl": "level", "abv": "above", "w": "write"}


class CannotMeasure(RuntimeError):
    """The harness could not obtain a number. NEVER downgraded to a failed check.

    ⛔ "could not measure" and "measured, nothing wrong" must not exit the same
    way. A failed check means the discriminant misbehaved; this means the
    harness did not get far enough to have an opinion, and it exits 3.
    """


def load_module(path: Path, name: str):
    """Import the tool, under `drive()`'s rule: any failure is CannotMeasure."""
    if not drive("checking the tool exists", path.is_file):
        raise CannotMeasure(f"tool not found: {path}")
    # ⛔ The whole body is inside the guard, per `drive()`'s rule — not just the
    # line that broke last.
    # ⛔ `sys.path` and `sys.modules` are restored to what they were before this
    # call, the tool's own entry included; the caller holds the module object.
    # The tool inserts two path entries at module scope, so repeated calls
    # accumulate them; and its sibling `_lib_compat`, left cached, makes a
    # second `--tool` copy silently reuse the first copy's. ⚠️ check5 does NOT
    # re-import per mutant — it monkey-patches the one imported module — so
    # today the only repeat caller is an in-process test or a second `--tool`.
    saved_path, saved_mods = list(sys.path), set(sys.modules)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise CannotMeasure(
                f"{path} is not importable as a Python module — importlib has"
                " no loader for it (wrong extension?)")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    except CannotMeasure:
        raise
    # ⛔ SystemExit is not an Exception; a `sys.exit()` at the tool's module
    # scope would otherwise walk out with no output and a code of its choosing.
    except (Exception, SystemExit) as exc:                  # noqa: BLE001
        raise CannotMeasure(
            f"importing {path} raised {type(exc).__name__}: {exc}") from exc
    finally:
        sys.path[:] = saved_path
        for k in set(sys.modules) - saved_mods:
            sys.modules.pop(k, None)
    return mod


def table_roles(path: Path):
    """[(label, role)] in render_ci's row order, read out of the tool's AST."""
    src = drive("reading the tool source",
                lambda: path.read_text(encoding="utf-8"))
    tree = drive("parsing the tool source", ast.parse, src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "render_ci"), None)
    if fn is None:
        raise CannotMeasure(f"no render_ci in {path}")
    for node in ast.walk(fn):
        if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Tuple):
            continue
        elts = node.iter.elts
        pairs = [(e.elts[0].value, e.elts[1].id) for e in elts
                 if isinstance(e, ast.Tuple) and len(e.elts) == 2
                 and isinstance(e.elts[0], ast.Constant)
                 and isinstance(e.elts[1], ast.Name)]
        if pairs and len(pairs) == len(elts):
            unknown = [v for _, v in pairs if v not in VAR_ROLE]
            if unknown:
                raise CannotMeasure(
                    f"render_ci's table binds unknown local(s) {unknown}; this"
                    " harness cannot tell which row is which. Update VAR_ROLE.")
            roles = [(lbl, VAR_ROLE[v]) for lbl, v in pairs]
            # ⛔ A row REMOVED from the table shrinks this list and the printed
            # table together, so the `len(body) != len(roles)` check downstream
            # sees nothing — and the checks then hit a bare `KeyError` at rc 1.
            # Measured by blind review of the finished file, which is where that
            # lockstep is visible; a per-round diff cannot see it.
            missing = sorted(set(VAR_ROLE.values()) - {r for _, r in roles})
            if missing:
                raise CannotMeasure(
                    f"render_ci's table no longer has a row for {missing};"
                    " every check here reads all three components")
            return roles
    raise CannotMeasure("could not locate render_ci's component table loop")


# ── the data producer (Go's probeReport, in Python) ──────────────────────────

def probe_q(sorted_ns, q):
    return sorted_ns[int(q * (len(sorted_ns) - 1))]


def probe_row(rnd, bench_n, write, load):
    w, l = sorted(write), sorted(load)
    return ("PROBEROW round={} bench_n={} iters={}"
            " write_p50={} write_p90={} write_p99={} write_max={} write_sum={}"
            " load_p50={} load_p90={} load_p99={} load_max={} load_sum={}").format(
        rnd, bench_n, len(write),
        probe_q(w, .50), probe_q(w, .90), probe_q(w, .99), w[-1], sum(write),
        probe_q(l, .50), probe_q(l, .90), probe_q(l, .99), l[-1], sum(load))


def base_profile():
    tail_n = ITERS - BULK_N
    bulk = [round(BULK_LO_NS + (BULK_HI_NS - BULK_LO_NS) * i / (BULK_N - 1))
            for i in range(BULK_N)]
    tail = [round(TAIL_LO_NS + (TAIL_HI_NS - TAIL_LO_NS) * i / (tail_n - 1))
            for i in range(tail_n)]
    return bulk, tail


def synth(shape):
    """`(text, totals)` — one synthetic probe log, and the round totals in it.

    ⛔ `totals` is `write_sum + load_sum` per MEASUREMENT round, from the
    numbers this function just generated — the one quantity this harness
    computes for itself. The boundary is deliberate: a sum of two fields the
    producer emits is a DEFINITION, not the judgement under test. It is what
    anchors the sd and median columns in ABSOLUTE terms; every other comparison
    here is a ratio, and ratios cannot see a unit bug both renderers share.
    """
    bulk, tail = base_profile()
    rng = random.Random(SEED)
    totals = []
    out = [f"PROBEENV goos=linux goarch=amd64 numcpu=4 go=synthetic"
           f" iters={ITERS} shape={shape} seed={SEED} b.N=1"]
    # The calibration row the tool is required to drop. Present so this harness
    # exercises that filter rather than a log the real probe never emits.
    # ⛔ NOT in `totals`: the tool drops it, so a report that included it would
    # be the bug, not the baseline.
    out.append(probe_row(0, 1, [WRITE_NS] * ITERS, bulk + tail))
    for k in range(1, ROUNDS + 1):
        if shape == "level":
            # Every bulk iteration slower by the same amount; the tail untouched.
            # This is the README's "水位平移": p50 moves, p90/p99 do not.
            write = [WRITE_NS + round((rng.random() - 0.5) * 2 * WRITE_JITTER_NS)] * ITERS
            d = round(rng.random() * LEVEL_SPAN_NS)
            load = [v + d for v in bulk] + tail
        elif shape == "episode":
            # A few iterations stall hard; everything else is byte-identical
            # round to round. The stalled ones sort above the tail, so p50 drifts
            # only by however far it is pushed along the bulk — small, and
            # positive, which is what makes this shape interesting.
            write = [WRITE_NS + round((rng.random() - 0.5) * 2 * WRITE_JITTER_NS)] * ITERS
            m = int(rng.random() * (EPISODE_MAX + 1))
            load = [v + EPISODE_NS for v in bulk[:m]] + bulk[m:] + tail
        elif shape == "write":
            # The load half barely moves — just enough that `level` and `above`
            # have a defined sd instead of rendering as `nan`. The write half
            # carries the variation, and carries it in the TAIL.
            d = round(rng.random() * WRITE_SHAPE_LOAD_SPAN_NS)
            load = [v + d for v in bulk] + tail
            mw = int(rng.random() * (WRITE_EPISODE_MAX + 1))
            write = [WRITE_NS + WRITE_EPISODE_NS] * mw + [WRITE_NS] * (ITERS - mw)
        else:
            raise CannotMeasure(f"unknown shape {shape!r}")
        out.append(probe_row(k, BENCH_N, write, load))
        totals.append(sum(write) + sum(load))
    return "\n".join(out) + "\n", totals


def _predicted_shares():
    """What the split's algebra says the sd shares must be, ignoring write.

    Shape A: a bulk-only shift d moves level by ITERS*d, load_sum by BULK_N*d,
    and therefore above by -(ITERS-BULK_N)*d. Shares are those ratios.
    Shape B: one stall moves load_sum by EPISODE_NS and level by ITERS times the
    bulk's per-index slope (p50 slides one bulk index per stall).
    ⚠️ Independent write jitter inflates the round total's sd, so these are
    upper bounds; TOL_PP absorbs the gap. ⛔ No figure is put on that gap here —
    every run prints prediction and measurement side by side, which is where a
    number that nothing regenerates belongs.
    """
    slope = (BULK_HI_NS - BULK_LO_NS) / (BULK_N - 1)
    lvl_b = ITERS * slope
    return {
        "level": {"level": 100 * ITERS / BULK_N,
                  "above": 100 * (ITERS - BULK_N) / BULK_N},
        "episode": {"level": 100 * lvl_b / EPISODE_NS,
                    "above": 100 * (EPISODE_NS - lvl_b) / EPISODE_NS},
    }


# ── scraping the tool's own output ───────────────────────────────────────────

def _num(cell, what):
    cell = cell.strip().rstrip("%")
    try:
        return float(cell)
    except ValueError:
        raise CannotMeasure(f"{what}: cannot read a number out of {cell!r}")


def drive(what, fn, *a):
    """Call into the tool. ⛔ A crash in there is CannotMeasure, never a FAIL.

    ⛔ THE RULE, because four consecutive rounds of blind review each found one
    more statement outside the boundary and each fix only moved the boundary
    past the statement that had just broken:

        EVERY operation that touches the tool goes through this function.
        Resolving its path, reading its bytes, reading its source, parsing it,
        importing it, calling into it. Anything that is this harness's OWN logic
        does NOT, and is allowed to crash.

    That line is where the exit-code contract comes from: could-not-measure (3)
    is "the tool, or getting at it, failed"; a failed check (1) is "the tool ran
    and the discriminant misbehaved"; an unhandled traceback means this harness
    has a bug, which is a third thing and should look like one.
    """
    try:
        return fn(*a)
    except CannotMeasure:
        raise
    # ⛔ SystemExit is NOT an Exception. A stray `sys.exit(1)` inside the tool
    # escaped this handler, printed nothing at all, and handed the process an
    # exit code of the tool's choosing — colliding with every code in the
    # contract. Worse than the traceback it replaced: that at least named a
    # cause. KeyboardInterrupt is deliberately still allowed through.
    except (Exception, SystemExit) as exc:                  # noqa: BLE001
        raise CannotMeasure(
            f"{what} raised {type(exc).__name__}: {exc}") from exc


def _corr(cell, what):
    """A Pearson r, range-checked. Outside [-1, 1], this harness has no opinion.

    ⛔ An out-of-range value has TWO causes — the scraper reading the wrong cell,
    or the tool's `corr()` not returning a correlation — and the number alone
    does not say which. So it stays could-not-measure and the message names
    both; reporting it as a failed check would assert something unseen.
    ⚠️ The assertion elsewhere had only a lower bound, which let a swapped
    column read `+132.200` as "~+1". Hence the range check, not just `>=`.
    """
    v = _num(cell, what)
    if v == v and abs(v) > 1.0 + 1e-9:          # NaN is a legitimate reading
        raise CannotMeasure(
            f"{what}: {v} is not a correlation. Either this harness is reading"
            " the wrong cell, or the tool's corr() is not returning one — from"
            " the number alone there is no way to tell, so neither is claimed")
    return v


def run_ci(mod, text, roles):
    """{role: (corr, sd_ms, share_pct)} scraped from render_ci's table."""
    session = drive("the tool's load()", mod.load, text, "synthetic")
    why = drive("the tool's reject()", mod.reject, session)
    if why:
        raise CannotMeasure(f"the tool refused the synthetic log: {why}")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        drive("the tool's render_ci()", mod.render_ci, session)
    lines = buf.getvalue().splitlines()
    at = next((i for i, s in enumerate(lines)
               if s.startswith("|") and "corr(round total" in s), None)
    if at is None:
        raise CannotMeasure("render_ci printed no component table")
    body = []
    for s in lines[at + 2:]:
        if not s.startswith("|"):
            break
        body.append([c.strip() for c in s.strip("|").split("|")])
    if len(body) != len(roles):
        raise CannotMeasure(f"render_ci printed {len(body)} table rows,"
                            f" the AST says {len(roles)}")
    out = {}
    for (label, role), cells in zip(roles, body):
        if cells[0] != label:
            raise CannotMeasure(f"row label {cells[0]!r} != AST's {label!r}")
        # ⛔ The scraping itself. `drive()` only wraps calls INTO the tool, so a
        # table the tool renders successfully but in an unexpected shape (a row
        # with two cells instead of four) crashed HERE — bare IndexError, rc 1,
        # the "a check failed" code. Found by blind review; the harness failing
        # to read something is could-not-measure, whoever's fault it is.
        try:
            out[role] = (_corr(cells[1], f"{role} corr"),
                         _num(cells[2].removesuffix("ms"), f"{role} sd"),
                         _num(cells[3], f"{role} share"))
        except CannotMeasure:
            raise
        except Exception as exc:                            # noqa: BLE001
            raise CannotMeasure(
                f"row {label!r} has {len(cells)} cell(s), not the 4 this"
                f" harness reads ({type(exc).__name__})") from exc
    return out


def run_archive(mod, text):
    """{role: (corr, share_pct, sd_ms)} + the round total's sd, from the archive."""
    session = drive("the tool's load()", mod.load, text, "synthetic")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        drive("the tool's render_archive()", mod.render_archive, [session])
    corrs, shares, sds, sd_total, med_total = None, {}, {}, None, None
    keys = (("level", "level  (p50 x iters)"), ("above", "above-p50 mass"),
            ("write", "write_sum "))
    for s in buf.getvalue().splitlines():
        t = s.strip()
        if t.startswith("POOLED"):
            f = t.split()
            corrs = dict(zip(("level", "above", "write"),
                             (_corr(x, "pooled corr") for x in f[2:5])))
        # ⛔ TWO printed lines start with "round total": the per-dispatch median
        # and the sd. Matching on the prefix alone left which one won to
        # iteration order. Disambiguated on the text each line actually carries.
        if t.startswith("round total") and " ms" in t:
            if "median" in t:
                med_total = _num(t.split(" ms")[0].split()[-1], "archive total median")
            elif "(range" in t:
                sd_total = _num(t.split(" ms")[0].split()[-1], "archive total sd")
        for role, prefix in keys:
            if t.startswith(prefix) and "=" in t:
                shares[role] = _num(t.rsplit("=", 1)[1].split()[0],
                                    f"archive {role} share")
                sds[role] = _num(t.split(" ms")[0].split()[-1], f"archive {role} sd")
    if (corrs is None or len(shares) != 3 or len(sds) != 3
            or sd_total is None or med_total is None):
        raise CannotMeasure("render_archive's pooled block did not yield the"
                            f" expected values (corrs={corrs is not None},"
                            f" shares={len(shares)}, sds={len(sds)},"
                            f" total sd={sd_total is not None},"
                            f" total median={med_total is not None})")
    return {r: (corrs[r], shares[r], sds[r]) for r in corrs}, sd_total, med_total


# ── checks ───────────────────────────────────────────────────────────────────

def check1(mod, path, roles):
    need = ("load", "reject", "render_ci", "render_archive", "level", "above",
            "total", "corr")
    missing = [n for n in need if not hasattr(mod, n)]
    if missing:
        raise CannotMeasure(f"{path} is missing {missing}")
    labels = ", ".join(f"{lbl!r}->{role}" for lbl, role in roles)
    return True, f"drives {len(need)} entry points; table rows {labels}"


def check2(ci):
    p = _predicted_shares()["level"]
    bad = []
    if ci["level"][0] < 1 - TOL_CORR:
        bad.append(f"level corr {ci['level'][0]:+.3f} is not ~+1")
    if ci["above"][0] > -(1 - TOL_CORR):
        bad.append(f"above corr {ci['above'][0]:+.3f} is not ~-1")
    for role in ("level", "above"):
        got, want = ci[role][2], p[role]
        if abs(got - want) > TOL_PP:
            bad.append(f"{role} share {got:.1f}% != predicted {want:.1f}%")
    detail = (f"corr level {ci['level'][0]:+.3f} / above {ci['above'][0]:+.3f};"
              f" shares {ci['level'][2]:.1f}% (predicted {p['level']:.1f}%)"
              f" and {ci['above'][2]:.1f}% (predicted {p['above']:.1f}%);"
              f" write {ci['write'][0]:+.3f} / {ci['write'][2]:.1f}%")
    return not bad, detail + ("" if not bad else "  << " + "; ".join(bad))


def check3(ci):
    p = _predicted_shares()["episode"]
    bad = []
    for role in ("level", "above"):
        if ci[role][0] < 1 - TOL_CORR:
            bad.append(f"{role} corr {ci[role][0]:+.3f} is not ~+1")
        got, want = ci[role][2], p[role]
        if abs(got - want) > TOL_PP:
            bad.append(f"{role} share {got:.1f}% != predicted {want:.1f}%")
    # ⛔ An episode is a load-side event, so a report that lets `write_sum` look
    # like a cause of it is wrong in the direction #1497 cares about most.
    if abs(ci["write"][0]) > QUIET_CORR or ci["write"][2] > QUIET_SHARE_PP:
        bad.append(f"write {ci['write'][0]:+.3f} / {ci['write'][2]:.1f}% is not"
                   " quiet on a load-side episode")
    detail = (f"corr level {ci['level'][0]:+.3f} / above {ci['above'][0]:+.3f}"
              f" — SAME SIGN, both ~+1; shares {ci['level'][2]:.1f}%"
              f" (predicted {p['level']:.1f}%) vs {ci['above'][2]:.1f}%"
              f" (predicted {p['above']:.1f}%); write stays quiet at"
              f" {ci['write'][0]:+.3f} / {ci['write'][2]:.1f}%")
    return not bad, detail + ("" if not bad else "  << " + "; ".join(bad))


def check4(lvl_ci, epi_ci):
    """Neither column alone separates the shapes; the pair does."""
    d_corr_level = abs(lvl_ci["level"][0] - epi_ci["level"][0])
    d_share_level = abs(lvl_ci["level"][2] - epi_ci["level"][2])
    signs_differ = (lvl_ci["above"][0] < 0) != (epi_ci["above"][0] < 0)
    bad = []
    if d_corr_level > 0.05:
        bad.append(f"the level row's corr already separates them"
                   f" (delta {d_corr_level:.3f}) — column 2's premise is wrong")
    if d_share_level < 100:
        bad.append(f"level share delta {d_share_level:.1f} pp is not decisive")
    if not signs_differ:
        bad.append("the above row's corr has the same sign in both shapes")
    return not bad, (
        f"level corr {lvl_ci['level'][0]:+.3f} vs {epi_ci['level'][0]:+.3f}"
        f" (delta {d_corr_level:.3f} — does NOT separate);"
        f" level share {lvl_ci['level'][2]:.1f}% vs {epi_ci['level'][2]:.1f}%"
        f" (delta {d_share_level:.1f} pp — DOES);"
        f" above corr {lvl_ci['above'][0]:+.3f} vs {epi_ci['above'][0]:+.3f}"
        f" (sign flip: {signs_differ})"
        + ("" if not bad else "  << " + "; ".join(bad)))


def check7(ci):
    """The write half of `total()` is load-bearing, and a dropped half shows.

    ⛔ In shapes "level" and "episode" the write half is only noise, so a
    `total()` that drops it changes nothing any other check reads — measured:
    `total := load_sum` passed all of them. Here the variation IS the write
    half, so either half going missing moves the numbers.
    """
    bad = []
    if ci["write"][0] < 1 - TOL_CORR:
        bad.append(f"write corr {ci['write'][0]:+.3f} is not ~+1")
    if not 100 - WRITE_SHARE_TOL_PP <= ci["write"][2] <= 100 + WRITE_SHARE_TOL_PP:
        bad.append(f"write share {ci['write'][2]:.1f}% is not ~100%")
    for role in ("level", "above"):
        if ci[role][2] > QUIET_SHARE_PP or abs(ci[role][0]) > QUIET_CORR:
            bad.append(f"{role} {ci[role][0]:+.3f} / {ci[role][2]:.1f}% is not"
                       " quiet on a write-side round")
    return not bad, (
        f"write {ci['write'][0]:+.3f} / {ci['write'][2]:.1f}%; level"
        f" {ci['level'][0]:+.3f} / {ci['level'][2]:.1f}% and above"
        f" {ci['above'][0]:+.3f} / {ci['above'][2]:.1f}% stay quiet"
        + ("" if not bad else "  << " + "; ".join(bad)))


MUTANTS = (
    ("above := load_sum (forget to subtract the level)",
     "above", lambda r: r["load_sum"]),
    ("level := load_p50 (forget the x iters)",
     "level", lambda r: r["load_p50"]),
    ("above := level - load_sum (sign flip)",
     "above", lambda r: r["load_p50"] * r["iters"] - r["load_sum"]),
    ("total := load_sum (drop the write half of the round total)",
     "total", lambda r: r["load_sum"]),
    ("total := write_sum (drop the load half of the round total)",
     "total", lambda r: r["write_sum"]),
    # ⛔ Not a plausible regression — a deliberate out-of-range correlation, so
    # the "the mutant made the report unreadable" path in check5 is EXERCISED
    # rather than merely written. Blind review measured that branch as dead
    # under the mutants above. ⚠️ This comment said "the four mutants above"
    # when there were five; the count is gone rather than corrected, for the
    # same reason every other hand-typed count in this change is gone.
    ("corr := 5.0 (a value no correlation can take)",
     "corr", lambda x, y: 5.0),
)


def check5(mod, roles, texts, baseline):
    """Every one of checks 2-4 must be killed by at least one broken split.

    ⛔ The intentional-break dogfood, in-process and part of the run. "Passes on
    the real code" is not evidence of detection power; a green nothing can turn
    red is the failure mode this whole file exists to remove.

    ⛔ `baseline` is the verdicts on the UNMUTATED tool. A check already FAIL
    there is NOT ASSESSABLE — every mutant kills it trivially, and a kill count
    that cannot tell "the mutant broke it" from "it was already broken" is
    could-not-measure wearing measured's costume.
    """
    blocked = sorted(n for n, ok in baseline.items() if not ok)
    assessable = sorted(n for n, ok in baseline.items() if ok)
    killed = {n: [] for n in assessable}
    blanket_kills = {n: [] for n in assessable}
    for name, attr, fn in MUTANTS:
        orig = getattr(mod, attr)
        setattr(mod, attr, fn)
        try:
            lvl = run_ci(mod, texts["level"], roles)
            epi = run_ci(mod, texts["episode"], roles)
            wrt = run_ci(mod, texts["write"], roles)
            verdicts = {2: check2(lvl)[0], 3: check3(epi)[0],
                        4: check4(lvl, epi)[0], 7: check7(wrt)[0]}
            blanket = False
        except CannotMeasure:
            # ⛔ A mutant that makes the report unreadable IS detected, but is
            # counted SEPARATELY: it kills every check at once, including one
            # whose own logic reads nothing. Detection power requires a kill
            # where the report was READABLE and this check still said no.
            verdicts = {n: False for n in assessable}
            blanket = True
        finally:
            setattr(mod, attr, orig)
        for n in assessable:
            if not verdicts[n]:
                (blanket_kills if blanket else killed)[n].append(
                    name.split(" (")[0])
    blind = [n for n, ks in killed.items() if not ks]
    rows = "; ".join(f"check{n} killed by {len(ks)}"
                     f"(+{len(blanket_kills[n])} unreadable)/{len(MUTANTS)}"
                     for n, ks in sorted(killed.items())) or "nothing assessable"
    # ⛔ Kill COUNTS answer "does each check detect anything". They do not answer
    # "does each check detect anything the others miss", or "does each mutant
    # probe anything another does not" — and blind review found both answered
    # NO in places. Neither is a pass/fail here; both are printed, because a
    # sentence claiming it would drift while a computed line cannot.
    for a in sorted(killed):
        for b in sorted(killed):
            if a != b and killed[a] and set(killed[a]) <= set(killed[b]):
                rows += f"; ⚠️ check{a} never fails without check{b}"
    by_mutant = {}
    for n, ks in killed.items():
        for k in ks:
            by_mutant.setdefault(k, set()).add(n)
    groups = {}
    for m, checks in by_mutant.items():
        groups.setdefault(frozenset(checks), []).append(m)
    for g in groups.values():
        if len(g) > 1:
            rows += f"; ⚠️ same kill set: {' == '.join(sorted(g))}"
    if blocked:
        rows += (f"  << check(s) {blocked} NOT ASSESSABLE: already FAIL on the"
                 " unmutated tool, so every mutant kills them vacuously")
    if blind:
        rows += (f"  << check(s) {blind} were killed only by mutants that made"
                 " the report unreadable, or by none — no demonstrated detection"
                 " power of their own")
    return not (blind or blocked), rows


def check6(cis, archs, totals):
    """Run the renderer cross-check over EVERY shape, not just one.

    ⛔ Blind review enumerated which scraped cells no check reads: pinning one
    shape left the other two's sd column unasserted — the same defect this
    check was added to close, in the shapes it did not cover.
    """
    bad, seen = [], []
    for shape in sorted(cis):
        ok, detail = _check6_one(cis[shape], archs[shape], totals[shape])
        seen.append(f"{shape}: {'ok' if ok else detail}")
        if not ok:
            bad.append(shape)
    return not bad, "; ".join(seen)


def _check6_one(ci, arch_pair, totals):
    """The two renderers agree, and each one's sd column matches its own share.

    ⛔ A number this file scrapes but never asserts is a number it is not
    checking, however prominently the report shows it — measured: changing only
    `render_ci`'s sd divisor (`/1e6` -> `/1e3`) used to pass everything.
    """
    arch, sd_total, med_total = arch_pair
    bad = []
    # ⛔ The MEDIAN anchor, not just the sd. sd and corr are both
    # SHIFT-invariant, so `total(r) + 999_999_999` — a whole second added to
    # every round — passed all seven checks unchanged. The median is the one
    # printed number an additive bias moves. Found by blind review.
    # ⚠️ The same class in `level()` / `above()` is NOT covered and cannot be:
    # neither raw value is ever printed, only their corr and sd, so an additive
    # bias there is invisible in the tool's output to any consumer, not just to
    # this harness. `test_level_and_above_are_only_ever_read_through_corr_or_sd`
    # does not exist; the claim is checkable by eye in `render_ci` /
    # `render_archive`, and is written down rather than asserted.
    want_med = st.median(totals) / 1e6
    if abs(med_total - want_med) > max(0.11, SD_ABS_TOL_FRAC * want_med):
        bad.append(f"round-total median {med_total:.1f} ms != {want_med:.1f} ms"
                   " computed from the rows this harness generated")
    # ⛔ The absolute anchor. Everything below this line is a RATIO, and blind
    # review proved ratios cannot see a unit bug applied consistently in both
    # renderers: `ms(x) = x/1e3` printed a round-total sd 1000x too large and
    # the whole run passed. `totals` comes from the generator, in ns.
    want_ms = st.stdev(totals) / 1e6
    if abs(sd_total - want_ms) > max(0.011, SD_ABS_TOL_FRAC * want_ms):
        bad.append(f"round-total sd {sd_total:.2f} ms != {want_ms:.2f} ms"
                   " computed from the rows this harness generated")
    for role in ("level", "above", "write"):
        if abs(ci[role][0] - arch[role][0]) > 0.0011:
            bad.append(f"{role} corr {ci[role][0]:+.3f} vs {arch[role][0]:+.3f}")
        if abs(ci[role][2] - arch[role][1]) > 0.11:
            bad.append(f"{role} share {ci[role][2]:.1f}% vs {arch[role][1]:.1f}%")
        if abs(ci[role][1] - arch[role][2]) > 0.011:
            bad.append(f"{role} sd {ci[role][1]:.2f} vs {arch[role][2]:.2f} ms")
        # The sd column and the share column are the same measurement printed
        # twice; a unit or scaling bug in either one breaks the identity.
        want = 100 * arch[role][2] / sd_total if sd_total else float("nan")
        if abs(ci[role][2] - want) > SD_SHARE_IDENTITY_TOL_PP:
            bad.append(f"{role} share {ci[role][2]:.1f}% != sd/total"
                       f" {want:.1f}% (sd {arch[role][2]:.2f} /"
                       f" {sd_total:.2f} ms)")
    return not bad, ("3 x (corr, sd, share) agree; share == sd / round-total sd"
                     if not bad else "; ".join(bad))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", type=Path, default=DEFAULT_TOOL)
    ap.add_argument("--dump", choices=("level", "episode", "write"),
                    help="print the synthetic log for one shape and exit")
    args = ap.parse_args(argv)

    if args.dump:
        sys.stdout.write(synth(args.dump)[0])
        return 0

    try:
        path = drive("resolving the tool path", args.tool.resolve)
        blob = drive("reading the tool file",
                     lambda: path.read_bytes() if path.is_file() else b"")
        print("=" * 74)
        print("IDENTITY OF THE THING MEASURED")
        print("=" * 74)
        print(f"  tool    {path}")
        print(f"  sha256  {hashlib.sha256(blob).hexdigest() if blob else '<unreadable>'}"
              f"  ({len(blob)} bytes)")
        mod = load_module(path, "analyze_probe_cf")
        roles = table_roles(path)
        bulk, tail = base_profile()
        prof = sorted(bulk + tail)
        print("\n  synthetic construction (NOT a replay of any recorded run):")
        print(f"      seed={SEED} rounds={ROUNDS} iters={ITERS} bench_n={BENCH_N}")
        print(f"      bulk  {BULK_N} iters, {BULK_LO_NS/1e6:.2f}..{BULK_HI_NS/1e6:.2f} ms")
        print(f"      tail  {ITERS-BULK_N} iters, {TAIL_LO_NS/1e6:.2f}..{TAIL_HI_NS/1e6:.2f} ms")
        print(f"      base profile quantiles: p50={probe_q(prof,.5)/1e6:.3f}"
              f" p90={probe_q(prof,.9)/1e6:.3f} p99={probe_q(prof,.99)/1e6:.3f}"
              f" max={prof[-1]/1e6:.3f} ms")
        print(f"      shape 'level'    bulk shifted by U[0,{LEVEL_SPAN_NS/1e6:.2f}) ms,"
              " tail untouched")
        print(f"      shape 'episode'  U[0,{EPISODE_MAX}] iters stalled by"
              f" {EPISODE_NS/1e6:.1f} ms, rest identical")
        print(f"      shape 'write'    U[0,{WRITE_EPISODE_MAX}] WRITES stalled by"
              f" {WRITE_EPISODE_NS/1e6:.1f} ms; load jitters by"
              f" U[0,{WRITE_SHAPE_LOAD_SPAN_NS/1000:.0f}) us/iter")
        print(f"      write baseline  {WRITE_NS/1000:.0f} us/iter; in 'level' and"
              f" 'episode' it jitters +-{WRITE_JITTER_NS/1000:.0f} us per round,"
              " independent of load")
        print(f"      tolerances: shares +-{TOL_PP} pp, |corr| within {TOL_CORR}\n")

        built = {s: synth(s) for s in ("level", "episode", "write")}
        texts = {s: v[0] for s, v in built.items()}
        totals = {s: v[1] for s, v in built.items()}
        results = [("1  tool exposes what this harness drives",
                    check1(mod, path, roles))]
        cis = {sh: run_ci(mod, texts[sh], roles) for sh in texts}
        archs = {sh: run_archive(mod, texts[sh]) for sh in texts}
        lvl, epi, wrt = cis["level"], cis["episode"], cis["write"]
        r2, r3, r4, r7 = check2(lvl), check3(epi), check4(lvl, epi), check7(wrt)
        baseline = {2: r2[0], 3: r3[0], 4: r4[0], 7: r7[0]}
        results += [
            ("2  pure level shift: shares match the split's algebra", r2),
            ("3  pure episode: level corr ~+1 yet a small share", r3),
            ("4  neither column alone separates the shapes", r4),
            ("7  write-side variation: the round total keeps both halves", r7),
            ("5  mutation sensitivity of checks 2-4 and 7",
             check5(mod, roles, texts, baseline)),
            ("6  renderers agree and the sd column is anchored, every shape",
             check6(cis, archs, totals)),
        ]
    except CannotMeasure as exc:
        print(f"\n::error::COULD NOT MEASURE — {exc}", file=sys.stderr)
        print("⛔ This is not a failed check. The harness never got far enough to"
              " have an opinion; do not read it as green.", file=sys.stderr)
        return 3

    print("=" * 74)
    print("RESULTS")
    print("=" * 74)
    for name, (ok, detail) in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    n = sum(ok for _, (ok, _) in results)
    print(f"\n{n}/{len(results)} PASS")
    return 0 if n == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
