#!/usr/bin/env python3
"""Turn one night's PAIRED benchmark run into per-benchmark ratios (ADR-032).

    python3 scripts/tools/dx/pair_bench_ratio.py \
        --reference .build/bench-ref.txt \
        --main      .build/bench-main.txt \
        --reference-tag exporter/v2.9.0 \
        --out       .build/bench-paired.json

WHAT THIS IS FOR
================
The nightly used to compare tonight's absolute numbers against a MEDIAN OF
OLDER NIGHTS. That reference slides: a permanent regression ages into it and
becomes the new normal, which is how a still-regressed issue gets auto-closed.

ADR-032 replaces it with a ratio measured inside a single night, on a single
machine:

    ratio = median(main side) / median(reference side)

Both sides ran on the same runner in the same job, so the machine's speed
appears in numerator and denominator alike and cancels. This script does only
that arithmetic — it does not decide anything. Deciding is the watchdog's job.

THREE STATES, NOT TWO
=====================
A benchmark that could not be evaluated is NOT the same as a benchmark that was
evaluated and found clean. Conflating them is the exact defect ADR-032's open
question 5
exists to prevent: a missing denominator read as "no findings" makes a
regression silent. So every benchmark lands in exactly one bucket:

    evaluated     both sides measured it → ratio present
    inconclusive  present on one side only → ratio ABSENT + a reason

`missing-in-reference` is the expected state for a benchmark added to main
after the reference version was cut. It is data, not an error, so this script
still exits 0 — but it never lets that benchmark look clean.

WORKLOAD DRIFT (`--workload-drift`)
===================================
The ratio only means "same work, different implementation" while both sides
define the SAME work. They do not: the reference is pinned for weeks to months
while main's benchmark test files keep being edited, so the two sides' FIXTURES
diverge over the reference's lifetime. Measured on the first real night, all
four of the exporter's `*bench_test.go` files had changed since the reference
tag, and one of those edits (a metric renamed to a 12-character-longer string,
in a benchmark that hashes map keys) sits underneath the largest ratio of the
night. A fixture change produces a PERMANENT STEP in the ratio, which is
indistinguishable in shape from a real regression.

This script cannot tell the two apart and does not try. It records what the
caller measured so a human can, per ADR-032's decision to disclose rather than
either silence the benchmark (all four files drift, so silencing is a total
outage of the detector) or auto-re-baseline it (that needs per-bench step state,
which is what the reverted frozen-anchor prototype died of).

⚠️ The disclosure is a HEURISTIC, not a proof. Its scope is whatever the caller
enumerated — see `.github/bench-reference.yaml`'s `workload_closure`, which is a
derived glob plus a HAND-ENUMERATED helper list. A fixture helper nobody put on
that list can change a benchmark's workload without appearing anywhere here.

WORKLOAD DIGEST (`--workload-digest`)
=====================================
The drift list above SATURATES. Measured over three nights (2026-08-16/17/18)
it was the same four lines verbatim — all four `*bench_test.go` differ from the
reference — which maps to 20/20 nightly benchmarks while only ONE had a
persistent step. A list that points at everyone points at no one: precision 1/20.

The digest is the same fact compressed to something that can actually change.
Per side, one scalar over the closure's contents; consumers compare a night's
digest with the PREVIOUS night's to learn the one thing the list cannot tell
them — *tonight the work definition moved*, which is exactly when a new step in
the ratio is suspect.

⛔ This script does NOT compute that comparison, and deliberately holds no
cross-run state. It records tonight's digests; deriving the transition belongs
to whoever reads the series. ADR-032 already recorded what happens when a
nightly starts remembering per-benchmark state across runs (its frozen-baseline
section, where the frozen-anchor prototype died of exactly that), and
`bench-workload-effect.yaml` carries
the same rule. Recording a scalar is not remembering; comparing to last night
inside the nightly would be.

⚠️ Same heuristic caveat as the list, for the same reason: a digest over an
under-enumerated closure is a confident-looking scalar over the wrong set.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import statistics as st
import sys

# Same two sys.path inserts every sibling dx tool uses: parent for the repo
# layout where _lib_compat.py sits one directory up, self-dir for the flat
# Docker layout. Importing the carrier is what hardens stdout — without it
# `--help` can die on a legacy Windows console (cp950).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402

# Same shape analyze_bench_history.py parses, deliberately: both read the raw
# `go test -bench` stdout that bench_interleave.sh appends per round.
#   "BenchmarkScanDirHierarchical_1000-4   93   35422664 ns/op   ..."
_BENCH_RE = re.compile(
    r"^(Benchmark[A-Za-z0-9_]+)-\d+\s+\d+\s+(\d+(?:\.\d+)?)\s+ns/op\b"
)
_CPU_RE = re.compile(r"^cpu:\s*(\S.*?)\s*$")


def read_side(path: pathlib.Path) -> tuple[dict[str, list[float]], str | None]:
    """Collect every ns/op sample per benchmark, plus the runner's CPU model.

    bench_interleave.sh appends one invocation's stdout per round, so the
    goos/goarch/pkg/cpu headers repeat. That is harmless here for the same
    reason it is harmless in analyze_bench_history.py: one job runs on one
    machine, so the first `cpu:` line is as good as any.
    """
    samples: dict[str, list[float]] = {}
    cpu: str | None = None
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            m = _BENCH_RE.match(line)
            if m:
                samples.setdefault(m.group(1), []).append(float(m.group(2)))
                continue
            if cpu is None:
                c = _CPU_RE.match(line)
                if c:
                    cpu = c.group(1)
    return samples, cpu


def pair(ref: dict[str, list[float]],
         main: dict[str, list[float]]) -> tuple[dict, dict]:
    """Split every benchmark seen on either side into evaluated / inconclusive."""
    evaluated: dict[str, dict] = {}
    inconclusive: dict[str, str] = {}
    for bench in sorted(set(ref) | set(main)):
        if bench not in ref:
            inconclusive[bench] = "missing-in-reference"
            continue
        if bench not in main:
            inconclusive[bench] = "missing-in-main"
            continue
        ref_ns = st.median(ref[bench])
        main_ns = st.median(main[bench])
        if ref_ns <= 0:
            inconclusive[bench] = "reference-median-not-positive"
            continue
        evaluated[bench] = {
            "reference_ns": ref_ns,
            "main_ns": main_ns,
            "ratio": main_ns / ref_ns,
            "n_reference": len(ref[bench]),
            "n_main": len(main[bench]),
        }
    return evaluated, inconclusive


def read_workload_drift(path: pathlib.Path | None) -> dict:
    """Three states, for the same reason the verdict has three.

    "not checked" and "checked, nothing drifted" are different facts, and a
    consumer that cannot tell them apart will read a missing check as a clean
    bill of health — the exact conflation ADR-032 exists to stop. So the field
    always carries its own status:

        not-requested  the caller passed no --workload-drift
        checked        the list is complete as far as the caller's scope goes
        unreadable     the caller asked, and no usable list arrived

    `unreadable` covers every way the answer can fail to show up — the file is
    missing, unreadable, or not decodable. The caller deletes the file when its
    own comparison could not be trusted, so "missing" is a deliberate signal,
    not only an accident.

    ⛔ `unreadable` is NOT fatal, and the exception list below is what makes
    that true. `UnicodeDecodeError` is a `ValueError`, NOT an `OSError`: catching
    only `OSError` let a drift file with one non-UTF-8 byte crash the tool, which
    the workflow reads as "the ratios failed" and turns the whole night into
    INCONCLUSIVE. That is exactly the trade this function's docstring says it
    refuses — a working measurement thrown away over an annotation. Caught in
    review on PR #1455 and reproduced before fixing.
    """
    if path is None:
        return {"status": "not-requested", "files": []}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"[pair_bench_ratio] could not read --workload-drift {path} "
              f"({type(exc).__name__}) — recording the check as unreadable "
              f"rather than as clean", file=sys.stderr)
        return {"status": "unreadable", "files": []}
    return {"status": "checked",
            "files": sorted({ln.strip() for ln in text.splitlines() if ln.strip()})}


_DIGEST_SIDES = ("reference", "main")

# ⛔ Shape-check the hash, not just its presence. `sha256sum | cut -d' ' -f1`
# yields 64 lowercase hex characters; anything else means the caller's pipeline
# broke in a way that still produced a line — e.g. an error string, a truncated
# read, or a `cut` that took the wrong field. Accepting it would hash the
# garbage into a digest that renders exactly like a real one and gets compared
# across nights. Measured before this guard: `reference\tconfig_test.go\t
# not-a-hash` returned `status: checked` with a perfectly normal-looking digest.
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def read_workload_digest(path: pathlib.Path | None) -> dict:
    """Aggregate the caller's per-file hashes into one scalar per side.

    Same three states as `read_workload_drift`, for the same reason: "not
    digested" and "digested, here it is" are different facts, and a consumer
    that cannot tell them apart reads an absent check as a clean one.

    Input format, one record per line, TAB-separated — written by the caller
    while BOTH source trees are still on disk, which is the only moment this
    comparison is possible at all:

        <side>\\t<path relative to the app dir>\\t<sha256 of the file>

    ⛔ The per-file hashes come from the caller (`sha256sum`), not from here.
    This tool never touches the source trees: it reads bench output and does
    arithmetic. Handing it tree paths would make "which files count as the
    workload" a second, silently divergent definition — the closure is defined
    once, in `.github/bench-reference.yaml`.

    The aggregate is a hash over that side's own sorted `<path> <sha>` lines, so
    it moves when a file's CONTENT changes and equally when a file is ADDED or
    REMOVED. Both are work-definition changes; a digest blind to the second
    would go quiet exactly when a new benchmark file lands.

    ⛔ Malformed input is `unreadable`, never a partial digest. A digest built
    from half the closure is worse than no digest: it renders as a normal-looking
    scalar and every downstream comparison against it is meaningless. This is the
    same trade the drift reader makes, and the same one the whole ADR-032 line
    keeps re-learning — a wrong number that renders correctly is the failure mode.
    """
    if path is None:
        return {"status": "not-requested", "sides": {}, "files": []}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"[pair_bench_ratio] could not read --workload-digest {path} "
              f"({type(exc).__name__}) — recording the digest as unreadable "
              f"rather than as clean", file=sys.stderr)
        return {"status": "unreadable", "sides": {}, "files": []}

    per_side: dict[str, dict[str, str]] = {s: {} for s in _DIGEST_SIDES}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        parts = line.split("\t")
        if (len(parts) != 3 or parts[0] not in per_side or not all(parts)
                or not _SHA256_RE.fullmatch(parts[2])):
            print(f"[pair_bench_ratio] --workload-digest {path}:{lineno} is not "
                  f"`<side>\\t<path>\\t<sha256>` with side in {list(_DIGEST_SIDES)} "
                  f"and a 64-hex sha "
                  f"— recording the digest as unreadable rather than digesting "
                  f"part of the closure", file=sys.stderr)
            return {"status": "unreadable", "sides": {}, "files": []}
        side, rel, sha = parts
        if rel in per_side[side]:
            print(f"[pair_bench_ratio] --workload-digest {path}:{lineno} repeats "
                  f"{rel!r} for side {side!r} — a duplicated record means the "
                  f"caller's enumeration is not a set; recording as unreadable",
                  file=sys.stderr)
            return {"status": "unreadable", "sides": {}, "files": []}
        per_side[side][rel] = sha

    if not all(per_side[s] for s in _DIGEST_SIDES):
        empty = [s for s in _DIGEST_SIDES if not per_side[s]]
        print(f"[pair_bench_ratio] --workload-digest {path} carries no records "
              f"for {' and '.join(empty)} — a one-sided digest cannot be compared, "
              f"so it is unreadable, not clean", file=sys.stderr)
        return {"status": "unreadable", "sides": {}, "files": []}

    sides = {}
    for s in _DIGEST_SIDES:
        canon = "\n".join(f"{rel} {per_side[s][rel]}" for rel in sorted(per_side[s]))
        sides[s] = {
            "digest": hashlib.sha256(canon.encode("utf-8")).hexdigest(),
            "n_files": len(per_side[s]),
        }
    return {
        "status": "checked",
        "sides": sides,
        # Kept for audit. ⛔ Deliberately NOT surfaced in the run summary: the
        # per-file view is the saturating list this digest exists to replace,
        # and the residue experiment already measured what printing a
        # low-precision list does (2.3% — worse than the 1/20 it was fixing).
        "files": sorted({rel for s in _DIGEST_SIDES for rel in per_side[s]}),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", type=pathlib.Path, required=True,
                    help="bench output of the fixed reference version")
    ap.add_argument("--main", type=pathlib.Path, required=True,
                    help="bench output of main HEAD, same runner, same job")
    ap.add_argument("--reference-tag", required=True,
                    help="the tag the reference side was built from")
    # A tag is a movable label; the SHA is what was actually checked out. The
    # pin file records both for exactly that reason, and a series of nights
    # that only names the tag cannot be re-read after a tag is re-pointed.
    ap.add_argument("--reference-sha", default=None,
                    help="the commit the reference side was built from "
                         "(recorded verbatim; the tag alone is re-pointable)")
    ap.add_argument("--workload-drift", type=pathlib.Path, default=None,
                    metavar="FILE",
                    help="newline-delimited benchmark test files that differ "
                         "between the two sides; disclosed, never acted on")
    ap.add_argument("--workload-digest", type=pathlib.Path, default=None,
                    metavar="FILE",
                    help="TAB-separated `<side>\t<relpath>\t<sha256>` records "
                         "over the workload closure; aggregated per side, never "
                         "compared across nights here")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()
    try_utf8_stdout()

    for p in (args.reference, args.main):
        if not p.is_file():
            print(f"[pair_bench_ratio] not a file: {p}", file=sys.stderr)
            return 2

    ref, ref_cpu = read_side(args.reference)
    main_s, main_cpu = read_side(args.main)
    if not ref or not main_s:
        print("[pair_bench_ratio] one side produced no benchmark rows — refusing "
              "to emit a paired verdict", file=sys.stderr)
        return 2

    # Both sides ran in one job on one runner. The `cpu:` header is the only
    # evidence of that in the data, so a side without one cannot be shown to
    # satisfy the pairing assumption. ⛔ Check for absence BEFORE comparing:
    # `None == None` is true, so two header-less sides would otherwise sail
    # through and publish ratios whose central premise was never checked.
    if ref_cpu is None or main_cpu is None:
        missing = [n for n, c in (("reference", ref_cpu), ("main", main_cpu))
                   if c is None]
        print(f"[pair_bench_ratio] no `cpu:` header on the {' and '.join(missing)} "
              f"side — the same-machine premise is unverifiable, so these are not "
              f"usable as a pair", file=sys.stderr)
        return 2

    # If the headers disagree, the pairing assumption (machine cancels) is
    # false and the ratio means nothing — fail loudly rather than publish a
    # number that looks fine.
    if ref_cpu != main_cpu:
        print(f"[pair_bench_ratio] the two sides report different CPUs "
              f"({ref_cpu!r} vs {main_cpu!r}) — that cannot happen within one "
              f"job, so the inputs are not a valid pair", file=sys.stderr)
        return 2

    evaluated, inconclusive = pair(ref, main_s)
    # `status` is present on BOTH paths. The workflow's unusable-measurement
    # step writes `"status": "INCONCLUSIVE"`, and a success payload that simply
    # omitted the key would force every consumer to encode "absent means OK" —
    # a default that is wrong the first time a third status exists.
    payload = {
        # v2: `workload_digest` joined the payload. Bumped rather than added
        # silently so a consumer pinned to v1 stops instead of quietly reading a
        # night whose work-definition evidence it does not know how to weigh.
        "schema": "bench-paired/v2",
        "status": "OK",
        "reference_tag": args.reference_tag,
        "reference_sha": args.reference_sha,
        "cpu": ref_cpu,
        "evaluated": evaluated,
        "inconclusive": inconclusive,
        "workload_drift": read_workload_drift(args.workload_drift),
        "workload_digest": read_workload_digest(args.workload_digest),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")

    print(f"[pair_bench_ratio] reference {args.reference_tag} on {ref_cpu}")
    print(f"[pair_bench_ratio] {len(evaluated)} evaluated, "
          f"{len(inconclusive)} inconclusive → {args.out}")
    for bench, d in sorted(evaluated.items(),
                           key=lambda kv: -abs(kv[1]["ratio"] - 1)):
        print(f"  {100 * (d['ratio'] - 1):+7.2f}%  {bench}")
    for bench, why in sorted(inconclusive.items()):
        print(f"  {'INCONCLUSIVE':>12}  {bench}  ({why})")
    drift = payload["workload_drift"]
    if drift["files"]:
        print(f"[pair_bench_ratio] workload drift — {len(drift['files'])} "
              f"benchmark test file(s) differ from the reference; ratios for the "
              f"benchmarks they define may be a changed workload, not a code change:")
        for f in drift["files"]:
            print(f"    {f}")
    elif drift["status"] != "checked":
        print(f"[pair_bench_ratio] workload drift NOT established "
              f"({drift['status']}) — the same-work premise is undisclosed, "
              f"not confirmed")
    dg = payload["workload_digest"]
    if dg["status"] == "checked":
        ref_d, main_d = dg["sides"]["reference"], dg["sides"]["main"]
        # Short prefixes: this line is for a human eyeballing two nights side by
        # side. The full digests are in the JSON. ⛔ No "changed"/"unchanged"
        # verdict here — this run has no idea what last night's was, and saying
        # otherwise would be the cross-run state the docstring refuses.
        print(f"[pair_bench_ratio] workload closure digest — "
              f"reference {ref_d['digest'][:12]} ({ref_d['n_files']} files), "
              f"main {main_d['digest'][:12]} ({main_d['n_files']} files); "
              f"compare with the previous night to see whether the work "
              f"definition moved")
    else:
        print(f"[pair_bench_ratio] workload closure digest NOT established "
              f"({dg['status']}) — tonight cannot be compared with any other "
              f"night's work definition")
    return 0


if __name__ == "__main__":
    sys.exit(main())
