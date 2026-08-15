#!/usr/bin/env python3
"""Negative validation for reproduce.py's estimators.

    python3 docs/internal/audit-reports/bench-xmachine-2026-08/selftest.py

Why this exists: the amplification ratio in ADR-032 is the whole basis for
accepting it, and an estimator that manufactures an amplification where none
exists would produce exactly the number the ADR wanted to see. This planted a
known machine effect and a known ABSENCE of one, and checks the estimator
recovers both. Case 2 is the important one — it is the failure mode that led an
earlier round of this investigation to a wrong conclusion.

It imports the real `estimators()` from reproduce.py rather than restating the
maths; a self-test that re-implements its subject validates the copy.
"""
from __future__ import annotations

import csv
import pathlib
import random
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from reproduce import estimators, load  # noqa: E402

BENCHES = {
    "BenchmarkResolveSilentModes_1000": 116_000.0,
    "BenchmarkScanDirFileHashes_1000": 2_400_000.0,
    "BenchmarkIncrementalLoad_1000_NoChange": 15_000_000.0,
    "BenchmarkFullDirLoad_1000": 255_000_000.0,
}
CPUS = ["AMD EPYC 7763 64-Core Processor", "AMD EPYC 9V74 80-Core Processor"]
DIGEST = "a" * 64


def synth(d: pathlib.Path, shards: int, machine_sd: float, jitter: float,
          seed: int, break_digest: bool = False) -> None:
    rng = random.Random(seed)
    with (d / "shard_meta.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["shard", "cpu", "nproc", "go_version", "binary_sha256"])
        for s in range(1, shards + 1):
            dig = ("b" * 64) if (break_digest and s == 2) else DIGEST
            w.writerow([s, CPUS[(s - 1) % len(CPUS)], 4, "go1.26.5", dig])
    with (d / "measurements.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["shard", "round", "side", "bench",
                    "ns_per_op_median", "n_samples"])
        for s in range(1, shards + 1):
            mult = 1.0 + rng.gauss(0.0, machine_sd)
            for r in range(1, 7):
                for side in "AB":
                    for b, base in BENCHES.items():
                        v = base * mult * (1.0 + rng.gauss(0.0, jitter))
                        w.writerow([s, r, side, b, f"{v:.1f}", 5])


def case(label, shards, machine_sd, jitter, seed, check):
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        synth(d, shards, machine_sd, jitter, seed)
        p, c, ev, tot = estimators(*load(d))
        amp = c / p if p else float("inf")
        ok, why = check(p, c, amp)
        print(f"{label:<44}PAIRED {100*p:6.3f}%  CROSS {100*c:6.3f}%  "
              f"amp {amp:6.1f}x  {'OK' if ok else 'FAIL — ' + why}")
        return ok


def main() -> int:
    fails = 0
    print("1. planted per-machine sd = 8% — the estimator must recover it")
    fails += not case("   machine_sd=0.08", 12, 0.08, 0.004, 2,
                      lambda p, c, a: (p < 0.01 and 0.03 < c < 0.16 and a > 5,
                                       "pairing must cancel a pure per-machine "
                                       "multiplier and CROSS must see it"))
    print("\n2. NO machine effect — the estimator must NOT invent one")
    fails += not case("   machine_sd=0", 12, 0.0, 0.004, 1,
                      lambda p, c, a: (a < 2.5,
                                       "amplification appeared with no machine "
                                       "effect present"))
    print("\n3. digest divergence must be refused, not analysed")
    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        synth(d, 6, 0.05, 0.004, 3, break_digest=True)
        meta, _ = load(d)
        n = len({m["binary_sha256"] for m in meta.values()})
        ok = n != 1
        print(f"{'   mismatched binaries detected':<44}distinct digests {n}  "
              f"{'OK' if ok else 'FAIL'}")
        fails += not ok
        # and the real entry point must exit non-zero on it
        shutil.copy(pathlib.Path(__file__).parent / "reproduce.py",
                    d / "reproduce.py")
        import subprocess
        rc = subprocess.run([sys.executable, "-B", str(d / "reproduce.py")],
                            capture_output=True, text=True).returncode
        print(f"{'   reproduce.py exit code':<44}{rc}  "
              f"{'OK' if rc == 2 else 'FAIL — must be 2'}")
        fails += rc != 2

    print(f"\n{'ALL CHECKS PASSED' if not fails else f'{fails} CHECK(S) FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
