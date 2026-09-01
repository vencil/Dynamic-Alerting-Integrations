#!/usr/bin/env python3
"""Negative validation for reproduce.py. No arguments; exits non-zero on failure.

    python3 docs/internal/audit-reports/bench-aa-realrunner-2026-08/selftest.py

`reproduce.py` reporting "1.67pp residual" only means something if the same
estimator would have reported something ELSE had the data been different. The
cases below plant known answers and check the estimator finds them --
including, and mainly, the case where the planted answer is "nothing", which
is what shows the estimator does not manufacture an effect out of noise.

Case 4 covers the other half: the digest gate must actually reject tampered
data, because every number here is only as good as "this is that run's bytes".

It imports reproduce.py rather than restating its arithmetic, so the thing
under test is the thing that ships.
"""
import copy
import importlib.util
import math
import os
import shutil
import statistics as st
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("rep", os.path.join(HERE, "reproduce.py"))
rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rep)

FAILURES = []


def check(name, ok, detail):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def scaled(per, factor):
    out = copy.deepcopy(per)
    for sides in out.values():
        sides["B"] = [x * factor for x in sides["B"]]
    return out


def main():
    # Verify the SHIPPED archive before loading it. Case 4 below only proves
    # the gate rejects a separately corrupted COPY; without this line a tamper
    # to the real raw/ would leave every case passing, because Cases 1-3 are
    # relative to whatever bytes happen to be there.
    names = rep.verify()
    per, _ = rep.load(names)

    print("Case 1 -- plant a known +8% effect on side B; the estimator must find it.")
    print("         Checked as a SHIFT off the measured baseline, not against +8.00%")
    print("         flat: this run's baseline is already -0.380% (side B ran second")
    print("         and came out faster), so an absolute check would fail for a")
    print("         reason that has nothing to do with the estimator.")
    base = st.mean(rep.paired(per, "ratio").values())
    got = st.mean(rep.paired(scaled(per, 1.08), "ratio").values())
    check("ratio form recovers +8%", abs((got - base) - 8.0) < 0.25,
          f"shifted {got - base:+.3f}pp (want +8.000pp; raw {base:+.3f} -> {got:+.3f})")
    # The log form is EXACTLY additive under a multiplicative plant, so this one
    # is asserted at machine precision rather than with a tolerance.
    base_log = st.mean(rep.paired(per, "log").values())
    got_log = st.mean(rep.paired(scaled(per, 1.08), "log").values())
    want_log = 100 * math.log(1.08)
    check("log form recovers +8% exactly", abs((got_log - base_log) - want_log) < 1e-9,
          f"shifted {got_log - base_log:.9f}pp (want {want_log:.9f}pp)")

    print("\nCase 2 -- plant NO effect (B := A); the estimator must report nothing.")
    print("         This is the case that matters: it shows the residual reported")
    print("         by reproduce.py is measured, not manufactured by the estimator.")
    nulled = copy.deepcopy(per)
    for sides in nulled.values():
        sides["B"] = list(sides["A"])
    vals = list(rep.paired(nulled, "ratio").values())
    check("mean is exactly zero", max(map(abs, vals)) == 0.0,
          f"largest |deviation| {max(map(abs, vals)):.3e}%")
    check("rSD is exactly zero", st.pstdev(vals) == 0.0, f"rSD {st.pstdev(vals):.3e}pp")

    print("\nCase 3 -- a 1% effect must NOT be swallowed by the measured residual")
    print("         (the residual is ~1.7pp, so this bounds what the instrument")
    print("          can and cannot resolve -- it is a limit, not a pass mark).")
    got1 = st.mean(rep.paired(scaled(per, 1.01), "ratio").values())
    check("+1% shifts the mean by +1%", abs((got1 - base) - 1.0) < 0.05,
          f"mean moved {got1 - base:+.3f}pp (want +1.000pp)")
    pooled_sd = st.pstdev(list(rep.paired(per, "ratio").values()))
    check("a single +1% comparison is INSIDE the noise", pooled_sd > 1.0,
          f"pooled rSD {pooled_sd:.2f}pp > 1.0pp, so one comparison cannot see it")

    print("\nCase 4 -- the digest gate must reject tampered bytes, not just report them")
    tmp = tempfile.mkdtemp()
    try:
        dst = os.path.join(tmp, "d")
        shutil.copytree(HERE, dst, ignore=shutil.ignore_patterns("__pycache__"))
        victim = os.path.join(dst, "raw", "r3_A.txt")
        data = bytearray(open(victim, "rb").read())
        data[-2] ^= 0x01  # flip one bit inside the last data line
        open(victim, "wb").write(bytes(data))
        proc = subprocess.run([sys.executable, os.path.join(dst, "reproduce.py")],
                              capture_output=True, text=True)
        check("tampered file -> exit 2", proc.returncode == 2,
              f"exit {proc.returncode} (want 2)")
        check("tampered file -> names the file", "r3_A.txt" in proc.stderr,
              "stderr names r3_A.txt" if "r3_A.txt" in proc.stderr else "stderr does not name it")

        shutil.copytree(HERE, dst + "2", ignore=shutil.ignore_patterns("__pycache__"))
        os.remove(os.path.join(dst + "2", "raw", "r7_B.txt"))
        proc = subprocess.run([sys.executable, os.path.join(dst + "2", "reproduce.py")],
                              capture_output=True, text=True)
        check("missing file -> exit 2", proc.returncode == 2,
              f"exit {proc.returncode} (want 2)")

        # An UNLISTED file is the direction that is easy to forget, and it was
        # measured to slip through an earlier version of verify(): 25 files
        # parsed, a 13th round in the output, "all SHA-256 verified" printed,
        # exit 0. Reported by CodeRabbit on PR #1669 and reproduced before fixing.
        shutil.copytree(HERE, dst + "3", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(os.path.join(dst + "3", "raw", "r1_A.txt"),
                    os.path.join(dst + "3", "raw", "r13_A.txt"))
        proc = subprocess.run([sys.executable, os.path.join(dst + "3", "reproduce.py")],
                              capture_output=True, text=True)
        check("file not in the manifest -> exit 2", proc.returncode == 2,
              f"exit {proc.returncode} (want 2)")
        check("unlisted file -> named in the error", "r13_A.txt" in proc.stderr,
              "stderr names r13_A.txt" if "r13_A.txt" in proc.stderr
              else "stderr does not name it")
        check("unlisted file -> no 13th round reported",
              "1..13" not in proc.stdout,
              "no 1..13 in stdout" if "1..13" not in proc.stdout
              else "stdout reported a 13th round")

        # A cross-check that DIFFs must fail loudly, not print DIFF and exit 0:
        # the section is the archive's identity proof.
        shutil.copytree(HERE, dst + "4", ignore=shutil.ignore_patterns("__pycache__"))
        rp = os.path.join(dst + "4", "reproduce.py")
        src = open(rp, encoding="utf-8").read().replace('"0.395"', '"9.999"')
        open(rp, "w", encoding="utf-8").write(src)
        proc = subprocess.run([sys.executable, rp], capture_output=True, text=True)
        check("cross-check difference -> non-zero exit", proc.returncode == 3,
              f"exit {proc.returncode} (want 3)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("all self-tests passed")


if __name__ == "__main__":
    main()
