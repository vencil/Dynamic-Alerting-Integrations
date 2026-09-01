#!/usr/bin/env python3
"""Recompute every number this directory's README claims. No arguments.

    python3 docs/internal/audit-reports/bench-aa-realrunner-2026-08/reproduce.py

Reads only the files next to it. No network, no benchmark re-runs. It answers
"can the README's numbers be recomputed from the archived data", NOT "what
would a fresh run produce today".

`raw/` holds the 24 bit-exact `go test -bench` outputs recovered from the job
log of run 31869902576; `raw_digests.csv` holds the SHA-256 the RUNNER printed
for each of them, so verification compares against the producer's digest and
not against a hash of the copy. A mismatch exits 2 rather than reporting
numbers computed from data that is not the data.

There is deliberately no measurements.csv: a derived table next to the raw
bytes is a second place for the same facts to live, and the two can drift.
Pass --csv to emit one on stdout instead.
"""
import csv
import hashlib
import math
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = "31869902576"
JOB = "94976812028"

# Both estimators appear below on purpose. The RATIO form is what
# `bench-aa-2026-08/analyze_aa.py` uses, so the two archives stay comparable.
# The LOG form is what the surviving prose in
# `.github/workflows/bench-xmachine-aa-experiment.yaml` was computed with --
# established here, not assumed: it is the only one of six variants that
# reproduces that file's three order-bias figures to the digit.
ESTIMATORS = {
    "ratio": lambda a, b: 100 * (b / a - 1),
    "log": lambda a, b: 100 * math.log(b / a),
}


def verify():
    """Digest-check the archive against the digests the runner printed.

    The manifest is AUTHORITATIVE in both directions, and the second direction
    is the one that is easy to leave out: checking only that every listed file
    verifies leaves a file that is present in raw/ but absent from the manifest
    completely unchecked -- and load() would still read it into the statistics.
    An unlisted r13_A.txt was measured to do exactly that: 25 files parsed, a
    13th round in the output, and "all SHA-256 verified" printed above it, exit
    0. So an extra file is a hard error, not a warning.

    Returns the verified file names; load() reads only those.
    """
    want = {}
    with open(os.path.join(HERE, "raw_digests.csv"), encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            want[row["file"]] = row["sha256"]
    bad = []
    for name, digest in sorted(want.items()):
        path = os.path.join(HERE, "raw", name)
        if not os.path.exists(path):
            bad.append((name, "MISSING", digest))
            continue
        got = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if got != digest:
            bad.append((name, got, digest))
    on_disk = {n for n in os.listdir(os.path.join(HERE, "raw")) if n.endswith(".txt")}
    for name in sorted(on_disk - set(want)):
        bad.append((name, "NOT IN MANIFEST", "-"))
    if bad:
        print(f"ARCHIVE VERIFICATION FAILED for {len(bad)} file(s):", file=sys.stderr)
        for name, got, digest in bad:
            print(f"  {name}: got {got} want {digest}", file=sys.stderr)
        sys.exit(2)
    return sorted(want)


def load(names=None):
    """-> {(bench, round): {side: [ns_op, ...]}}, plus the identity headers.

    `names` is the verified list from verify(). Defaulting it to a directory
    listing would reopen the hole verify() closes, so the default re-verifies
    rather than globbing.
    """
    per, ident = {}, {}
    for name in (verify() if names is None else names):
        stem = name[:-4]
        rnd, side = re.match(r"r(\d+)_([AB])$", stem).groups()
        text = open(os.path.join(HERE, "raw", name), encoding="utf-8").read()
        for key in ("goos", "goarch", "pkg", "cpu"):
            hit = re.search(rf"^{key}: (.*)$", text, re.M)
            if hit:
                ident.setdefault(key, set()).add(hit.group(1).strip())
        # The "-N" suffix is GOMAXPROCS, not part of the benchmark name; the
        # two numeric columns are b.N and ns/op in that order.
        for bench, _procs, _iters, ns in re.findall(
            r"^(Benchmark\S+?)-(\d+)\s+(\d+)\s+(\d+) ns/op$", text, re.M
        ):
            per.setdefault((bench, int(rnd)), {}).setdefault(side, []).append(float(ns))
        ident.setdefault("gomaxprocs", set()).update(
            m for m in re.findall(r"^Benchmark\S+?-(\d+)\s", text, re.M)
        )
    return per, ident


def paired(per, form):
    """One value per (bench, round): the A/A deviation from a true 1.000."""
    est = ESTIMATORS[form]
    out = {}
    for (bench, rnd), sides in per.items():
        if "A" in sides and "B" in sides:
            out[(bench, rnd)] = est(st.median(sides["A"]), st.median(sides["B"]))
    return out


def main():
    names = verify()
    per, ident = load(names)
    n = len(names)

    # Print what is under test BEFORE any comparison, so a number computed on
    # the wrong tree is visible as such rather than inferred to be right.
    print("=" * 72)
    print(f"IDENTITY  run {RUN} / job {JOB}  (bench-aa-noise-experiment.yaml)")
    for key in ("goos", "goarch", "pkg", "cpu", "gomaxprocs"):
        vals = sorted(ident.get(key, []))
        flag = "" if len(vals) == 1 else "   <-- NOT UNIQUE"
        print(f"  {key:<11}: {', '.join(vals)}{flag}")
    rounds = sorted({r for _, r in per})
    benches = sorted({b for b, _ in per})
    counts = {len(v) for sides in per.values() for v in sides.values()}
    print(f"  rounds     : {len(rounds)} ({min(rounds)}..{max(rounds)})")
    print(f"  benchmarks : {len(benches)}")
    print(f"  -test.count: {sorted(counts)}")
    print(f"  raw files  : {n}, all SHA-256 verified against the runner's digest")
    print("\n  Both sides run the SAME compiled binary (the workflow does one")
    print("  `go test -c`), so the true ratio is exactly 1.000 everywhere and")
    print("  every deviation below is measurement error.")

    for form in ("ratio", "log"):
        vals_by = paired(per, form)
        print("\n" + "=" * 72)
        print(f"PAIRED A/A RESIDUAL  [{form} form]")
        pooled = []
        for bench in benches:
            v = [vals_by[(bench, r)] for r in rounds if (bench, r) in vals_by]
            pooled += v
            print(
                f"  {bench:<40} n={len(v):2d}  rSD {st.pstdev(v):5.2f}pp  "
                f"max|dev| {max(map(abs, v)):5.2f}%  median {st.median(v):+6.2f}%"
            )
        mean = st.mean(pooled)
        sd = st.stdev(pooled)
        tstat = mean / (sd / math.sqrt(len(pooled)))
        print(
            f"  {'POOLED':<40} n={len(pooled):2d}  rSD {st.pstdev(pooled):5.2f}pp  "
            f"max|dev| {max(map(abs, pooled)):5.2f}%  median {st.median(pooled):+6.2f}%"
        )
        print(
            f"  |deviation| > 5%: "
            f"{sum(1 for x in pooled if abs(x) > 5)}/{len(pooled)}"
        )
        print(f"  mean {mean:+.3f}%   |t| {abs(tstat):.2f}   "
              f"A-faster {sum(1 for x in pooled if x > 0)}/{len(pooled)}")

    # The one cross-check that pins this archive to the claim it explains.
    print("\n" + "=" * 72)
    print("CROSS-CHECK against bench-xmachine-aa-experiment.yaml's order-bias")
    print("comment, which cites THIS run by number. Reproducing it to the digit")
    print("is what establishes that this archive is that run's data.")
    pooled = list(paired(per, "log").values())
    mean = st.mean(pooled)
    sd = st.stdev(pooled)
    tstat = abs(mean / (sd / math.sqrt(len(pooled))))
    checks = [
        ("B faster than A by 0.395% on average", f"{-mean:.3f}", "0.395"),
        ("48 paired observations", f"{len(pooled)}", "48"),
        ("|t| = 1.59", f"{tstat:.2f}", "1.59"),
        ("sign split 23/48", f"{sum(1 for x in pooled if x > 0)}/48", "23/48"),
    ]
    lean = {b: st.mean([v for (bb, _), v in paired(per, "log").items() if bb == b])
            for b in benches}
    same = len({x > 0 for x in lean.values()}) == 1
    checks.append(("all four benches leaned the same way",
                   "yes" if same else "no", "yes"))
    differing = []
    for label, got, want in checks:
        ok = got == want
        if not ok:
            differing.append(label)
        print(f"  {'OK ' if ok else 'DIFF'}  {label:<38} "
              f"computed {got:>7}  quoted {want}")
    # Printing DIFF and exiting 0 would make this section decorative: a caller
    # (or CI) reading only the exit code would treat "this archive is NOT that
    # run's data" as success. The cross-check is the archive's identity proof,
    # so a difference is a hard failure.
    if differing:
        print(f"\nCROSS-CHECK FAILED for {len(differing)} claim(s): "
              f"{', '.join(differing)}", file=sys.stderr)
        print("This archive does not reproduce the figures the workflow header "
              "attributes to this run.", file=sys.stderr)
        sys.exit(3)


def emit_csv():
    per, _ = load(verify())
    out = csv.writer(sys.stdout)
    out.writerow(["bench", "round", "side", "sample_index", "ns_per_op"])
    for (bench, rnd) in sorted(per):
        for side in sorted(per[(bench, rnd)]):
            for i, ns in enumerate(per[(bench, rnd)][side]):
                out.writerow([bench, rnd, side, i, ns])


if __name__ == "__main__":
    if "--csv" in sys.argv:
        emit_csv()
    else:
        main()
