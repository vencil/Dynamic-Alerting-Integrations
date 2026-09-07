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

⛔ THE OLD NUMBERS ARE NOT REPRODUCED AND THIS FILE DOES NOT TRY. §四 used to
quote 150.8% / 50.8% / +0.791 / 1.1%; the parameters that produced them are not
recoverable from the numbers alone. The synthetic shapes below were chosen for
being cleanly constructible and are printed in full at the top of every run —
they are NOT reverse-engineered to land on the old figures. Where the two
disagree, the number this file prints is the one that has a command behind it.
(As it happens 150.8/50.8 and this file's ITERS/BULK_N are the same algebra with
a different bulk fraction: 1/f and (1-f)/f, f=0.663 there, f=0.75 here. That is
an observation about the old numbers' shape, not a reproduction of them.)

Six checks:
  1. the tool exposes what this harness drives, and its identity is printed
  2. pure level shift: the shares match what the split's algebra predicts
  3. pure episode: `level` correlates at ~+1.000 and still accounts for ~5%
  4. neither column alone separates the two shapes; together they do
  5. mutation sensitivity — each of checks 2-4 is killed by >=1 broken
     discriminant, so "these checks have detection power" is measured
     here, and a check already red before any mutant is reported as NOT
     ASSESSABLE rather than credited with a vacuous kill
  6. the CI table and the archive block agree, to printed precision

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

# ⛔ WRITE_JITTER_NS exists so `write_sum` has a DEFINED correlation. With write
# constant across rounds its sd is 0 and the tool prints `+nan` there, which
# tells the reader nothing about the discriminant. It is independent of the load
# shape by construction, so it also serves as the negative control: a component
# that neither tracks nor could move the round has to come out looking like one.
# It costs the predictions below <1 pp — see _predicted_shares.

TOL_PP = 2.0                # tolerance on a predicted-vs-measured share, in pp
TOL_CORR = 0.01             # |corr| must be within this of 1.0 where predicted

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
    """Import the tool. ⛔ An import that raises is a CannotMeasure, not a FAIL.

    Found while dogfooding `--tool`: a copy of the tool placed somewhere without
    a `../_lib_compat.py` beside it (the tool inserts its own parent on
    `sys.path` and imports from there) came out as a bare ModuleNotFoundError
    traceback and rc=1 — the same exit code a discriminant that misbehaved gets.
    """
    if not path.is_file():
        raise CannotMeasure(f"tool not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                # noqa: BLE001
        sys.modules.pop(name, None)
        raise CannotMeasure(
            f"importing {path} raised {type(exc).__name__}: {exc}") from exc
    return mod


def table_roles(path: Path):
    """[(label, role)] in render_ci's row order, read out of the tool's AST."""
    fn = next((n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
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
            return [(lbl, VAR_ROLE[v]) for lbl, v in pairs]
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
    """One synthetic probe log of known shape, as the text `load()` consumes."""
    bulk, tail = base_profile()
    rng = random.Random(SEED)
    out = [f"PROBEENV goos=linux goarch=amd64 numcpu=4 go=synthetic"
           f" iters={ITERS} shape={shape} seed={SEED} b.N=1"]
    # The calibration row the tool is required to drop. Present so this harness
    # exercises that filter rather than a log the real probe never emits.
    out.append(probe_row(0, 1, [WRITE_NS] * ITERS, bulk + tail))
    for k in range(1, ROUNDS + 1):
        wj = round((rng.random() - 0.5) * 2 * WRITE_JITTER_NS)
        if shape == "level":
            # Every bulk iteration slower by the same amount; the tail untouched.
            # This is the README's "水位平移": p50 moves, p90/p99 do not.
            d = round(rng.random() * LEVEL_SPAN_NS)
            load = [v + d for v in bulk] + tail
        elif shape == "episode":
            # A few iterations stall hard; everything else is byte-identical
            # round to round. The stalled ones sort above the tail, so p50 drifts
            # only by however far it is pushed along the bulk — small, and
            # positive, which is what makes this shape interesting.
            m = int(rng.random() * (EPISODE_MAX + 1))
            load = [v + EPISODE_NS for v in bulk[:m]] + bulk[m:] + tail
        else:
            raise CannotMeasure(f"unknown shape {shape!r}")
        out.append(probe_row(k, BENCH_N, [WRITE_NS + wj] * ITERS, load))
    return "\n".join(out) + "\n"


def _predicted_shares():
    """What the split's algebra says the sd shares must be, ignoring write.

    Shape A: a bulk-only shift d moves level by ITERS*d, load_sum by BULK_N*d,
    and therefore above by -(ITERS-BULK_N)*d. Shares are those ratios.
    Shape B: one stall moves load_sum by EPISODE_NS and level by ITERS times the
    bulk's per-index slope (p50 slides one bulk index per stall).
    ⚠️ Independent write jitter inflates the round total's sd, so these are
    upper bounds; at the parameters above the gap is under 1 pp, which is why
    TOL_PP is 2.
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


def run_ci(mod, text, roles):
    """{role: (corr, sd_ms, share_pct)} scraped from render_ci's table."""
    session = mod.load(text, "synthetic")
    why = mod.reject(session)
    if why:
        raise CannotMeasure(f"the tool refused the synthetic log: {why}")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.render_ci(session)
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
        out[role] = (_num(cells[1], f"{role} corr"),
                     _num(cells[2].removesuffix("ms"), f"{role} sd"),
                     _num(cells[3], f"{role} share"))
    return out


def run_archive(mod, text):
    """{role: (corr, share_pct)} scraped from render_archive's pooled block."""
    session = mod.load(text, "synthetic")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        mod.render_archive([session])
    corrs, shares = None, {}
    keys = (("level", "level  (p50 x iters)"), ("above", "above-p50 mass"),
            ("write", "write_sum "))
    for s in buf.getvalue().splitlines():
        t = s.strip()
        if t.startswith("POOLED"):
            f = t.split()
            corrs = dict(zip(("level", "above", "write"),
                             (_num(x, "pooled corr") for x in f[2:5])))
        for role, prefix in keys:
            if t.startswith(prefix) and "=" in t:
                shares[role] = _num(t.rsplit("=", 1)[1].split()[0],
                                    f"archive {role} share")
    if corrs is None or len(shares) != 3:
        raise CannotMeasure("render_archive printed no pooled correlation row"
                            f" (corrs={corrs is not None}, shares={len(shares)})")
    return {r: (corrs[r], shares[r]) for r in corrs}


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
    detail = (f"corr level {ci['level'][0]:+.3f} / above {ci['above'][0]:+.3f}"
              f" — SAME SIGN, both ~+1; shares {ci['level'][2]:.1f}%"
              f" (predicted {p['level']:.1f}%) vs {ci['above'][2]:.1f}%"
              f" (predicted {p['above']:.1f}%)")
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


MUTANTS = (
    ("above := load_sum (forget to subtract the level)",
     "above", lambda r: r["load_sum"]),
    ("level := load_p50 (forget the x iters)",
     "level", lambda r: r["load_p50"]),
    ("above := level - load_sum (sign flip)",
     "above", lambda r: r["load_p50"] * r["iters"] - r["load_sum"]),
)


def check5(mod, roles, texts, baseline):
    """Every one of checks 2-4 must be killed by at least one broken split.

    ⛔ This is the intentional-break dogfood, in-process and part of the run
    rather than a thing someone did once by hand. A check no mutant can kill is
    reported as such and fails this check: "passes on the real code" is not
    evidence of detection power, and a green that nothing can turn red is the
    exact failure mode §四 documents.

    ⛔ `baseline` is the verdicts on the UNMUTATED tool, and a check that is
    already FAIL there is NOT ASSESSABLE — every mutant "kills" it trivially.
    Measured, not reasoned: the first draft took no baseline, and with
    `above := load_sum` written into the tool on disk it reported
    "check2 killed by 3/3" while check2 was red before any mutant ran. A kill
    count that cannot tell "the mutant broke it" from "it was already broken"
    is the same could-not-measure-wearing-measured costume this whole file is
    about.
    """
    blocked = sorted(n for n, ok in baseline.items() if not ok)
    assessable = sorted(n for n, ok in baseline.items() if ok)
    killed = {n: [] for n in assessable}
    for name, attr, fn in MUTANTS:
        orig = getattr(mod, attr)
        setattr(mod, attr, fn)
        try:
            lvl = run_ci(mod, texts["level"], roles)
            epi = run_ci(mod, texts["episode"], roles)
        finally:
            setattr(mod, attr, orig)
        verdicts = {2: check2(lvl)[0], 3: check3(epi)[0], 4: check4(lvl, epi)[0]}
        for n in assessable:
            if not verdicts[n]:
                killed[n].append(name.split(" (")[0])
    blind = [n for n, ks in killed.items() if not ks]
    rows = "; ".join(f"check{n} killed by {len(ks)}/{len(MUTANTS)}"
                     for n, ks in sorted(killed.items())) or "nothing assessable"
    if blocked:
        rows += (f"  << check(s) {blocked} NOT ASSESSABLE: already FAIL on the"
                 " unmutated tool, so every mutant kills them vacuously")
    if blind:
        rows += f"  << check(s) {blind} survive every mutant — no detection power"
    return not (blind or blocked), rows


def check6(ci, arch):
    """The CI table and the archive block are the same three quantities."""
    bad = []
    for role in ("level", "above", "write"):
        if abs(ci[role][0] - arch[role][0]) > 0.0011:
            bad.append(f"{role} corr {ci[role][0]:+.3f} vs {arch[role][0]:+.3f}")
        if abs(ci[role][2] - arch[role][1]) > 0.11:
            bad.append(f"{role} share {ci[role][2]:.1f}% vs {arch[role][1]:.1f}%")
    return not bad, ("3 components x (corr, sd share) agree to printed precision"
                     if not bad else "; ".join(bad))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tool", type=Path, default=DEFAULT_TOOL)
    ap.add_argument("--dump", choices=("level", "episode"),
                    help="print the synthetic log for one shape and exit")
    args = ap.parse_args(argv)

    if args.dump:
        sys.stdout.write(synth(args.dump))
        return 0

    try:
        path = args.tool.resolve()
        blob = path.read_bytes() if path.is_file() else b""
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
        print(f"      write  {WRITE_NS/1000:.0f} us/iter +- {WRITE_JITTER_NS/1000:.0f} us"
              " per round, independent of load")
        print(f"      tolerances: shares +-{TOL_PP} pp, |corr| within {TOL_CORR}\n")

        texts = {s: synth(s) for s in ("level", "episode")}
        results = [("1  tool exposes what this harness drives",
                    check1(mod, path, roles))]
        lvl = run_ci(mod, texts["level"], roles)
        epi = run_ci(mod, texts["episode"], roles)
        r2, r3, r4 = check2(lvl), check3(epi), check4(lvl, epi)
        baseline = {2: r2[0], 3: r3[0], 4: r4[0]}
        results += [
            ("2  pure level shift: shares match the split's algebra", r2),
            ("3  pure episode: level corr ~+1 yet a ~5% share", r3),
            ("4  neither column alone separates the shapes", r4),
            ("5  mutation sensitivity of checks 2-4",
             check5(mod, roles, texts, baseline)),
            ("6  CI table == archive block", check6(lvl, run_archive(mod, texts["level"]))),
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
