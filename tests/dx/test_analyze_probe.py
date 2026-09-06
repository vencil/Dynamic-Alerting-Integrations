"""Tests for analyze_probe.py — the de-duplicated probe summary (TRK-373 / #1731).

This module replaced two copies of one computation: an inline python program in
.github/workflows/bench-probe-write-latency.yaml and a script in
docs/internal/audit-reports/bench-probe-2026-09/. Nothing had been checking that
the two agreed, and they had drifted.

⛔ The fix deleted one copy rather than adding a copy-vs-copy test, which would
have locked the duplication in place. So there is deliberately no such assertion.

What is pinned here:

  1. GOLDEN — both renderers, byte for byte, against output captured before the
     merge. It is the only artefact that can still answer "did de-duplicating
     change what the reports say": the copy it was captured from is gone.
     ⚠️ Regenerating a golden SILENCES this. If a change is meant to alter a
     report, regenerate deliberately and say so; never to clear a red.

  2. THE RESOLVED DIVERGENCES — for each, the behaviour that was chosen, on an
     input that used to tell the two copies apart.
     ⚠️ Divergence 4 (NaN rendering) is also visible to the goldens, being a
     format string they already pin; 1, 2, 3, 5 and 6 each turn only their own
     test red.
     ⛔ The count is over what was enumerated, not a proof that no more exist —
     the replaced copy is gone, so completeness cannot be established now.

  3. THE REJECTION PATHS — rc=2 and an ::error:: line, not a traceback.
     ⚠️ Only the ones that already rejected. Malformed records still raise a
     bare traceback in both renderers; that is TRK-375 (#1733), deliberately not
     fixed here, and these tests pin today's behaviour so #1733 must change them
     on purpose.

⛔ WHERE THE HISTORY LIVES, AND WHY NOT HERE. Five review rounds on this change
produced 30 findings; 16 were defects in explanatory prose like this docstring,
and of the 13 found in the two rounds that reviewed a FIX, 9 were prose the
previous fix had just written. Rounds 3 and 5 found zero production defects.
An earlier version of this file carried that error history inline and it kept
generating new errors — stale counts, contradictions between paragraphs. So the
narrative moved to where the repo already keeps history: the CHANGELOG entry for
#1731 and dev/trk-373/ROUNDS.jsonl. What stays here is only what a reader needs
in order not to break something, and any number a command can produce is given
as the command instead.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "tools" / "dx" / "analyze_probe.py"
ARCHIVE = REPO / "docs" / "internal" / "audit-reports" / "bench-probe-2026-09"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "analyze_probe"
RUNS = ["probe-run1.txt", "probe-run2.txt", "probe-run3.txt"]


def run(*args, cwd=None):
    """Invoke the tool as the workflow does — a subprocess, not an import.

    ⛔ Deliberately a subprocess: what the workflow consumes is this program's
    stdout and exit status, so that is what gets asserted. An in-process call
    would test a different interface than the one CI depends on.

    ⚠️ Returns stdout and stderr COMBINED — right for assertions that look for a
    message, wrong for a byte-for-byte golden: the workflow tees only stdout, so
    a stderr line cannot change the published report yet would break a combined
    golden. The golden tests use `run_stdout` below.
    """
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd, timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def run_stdout(*args, cwd=None):
    """Same, but returns only stdout — the stream the workflow actually tees."""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd, timeout=120,
    )
    return proc.returncode, proc.stdout


def build_archive(tmp_path, mutate=None, files=RUNS, mutate_all=False):
    """Copy the real archive into tmp_path, optionally mutating probe-run1.txt.

    ⚠️ `mutate_all` is load-bearing for archive-mode assertions about POOLED
    quantities. With the default (probe-run1 only) the other two pristine files
    dilute the pool, so a mutation meant to drive e.g. a median to zero does not
    reach the branch under test and the assertion fails for a reason that has
    nothing to do with the code. Per-file assertions keep the default.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in files:
        lines = (ARCHIVE / name).read_text(encoding="utf-8").splitlines()
        if mutate is not None and (mutate_all or name == "probe-run1.txt"):
            lines = mutate(lines)
        (tmp_path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Golden — the de-duplication must not have changed either report
# ---------------------------------------------------------------------------

def test_the_archive_inputs_are_the_ones_the_goldens_were_recorded_from():
    """⛔ Runs BEFORE the goldens, and exists to separate two different failures.

    The goldens run against the live audit directory rather than a copied
    fixture — copying 70 KB of the same measurement data into `tests/` to protect
    a de-duplication change would put a second copy of it in the tree. The cost
    is that "the data changed" and "the code changed" would otherwise look like
    the same golden diff. This test separates them: if it fails, the INPUTS
    moved; if it passes and a golden fails, the CODE changed the report.
    """
    import hashlib

    digests = {
        "probe-run1.txt": "16610ef889f18a3f0526092d9c50f326",
        "probe-run2.txt": "4af2e47e0d4ec1fc9529e9833b791062",
        "probe-run3.txt": "20d08111dfce7df8d79eaed5acf9f573",
    }
    actual = {
        name: hashlib.md5((ARCHIVE / name).read_bytes()).hexdigest()
        for name in RUNS
    }
    assert actual == digests, (
        "the archived probe data changed. ⚠️ The goldens below may or may not "
        "also fail — the parser ignores non-record lines, so some edits move the "
        "bytes without moving the report. Work out whether the report actually "
        "changed before regenerating anything."
    )


def test_archive_report_matches_the_pre_change_golden():
    rc, out = run_stdout("--archive", str(ARCHIVE))
    assert rc == 0, out
    expected = (FIXTURES / "archive.golden.txt").read_text(encoding="utf-8")
    assert out == expected


def test_ci_summary_matches_the_pre_change_golden():
    rc, out = run_stdout("--from-log", str(ARCHIVE / "probe-run1.txt"))
    assert rc == 0, out
    expected = (FIXTURES / "ci-run1.golden.md").read_text(encoding="utf-8")
    assert out == expected


def test_every_archived_run_still_summarises_cleanly():
    """The golden pins run1 only; the other two must at least still parse.

    ⚠️ This is a weaker assertion on purpose. Pinning all three byte for byte
    would triple the fixture size to re-pin one renderer's formatting, which the
    run1 golden already covers.
    """
    for name in RUNS:
        rc, out = run("--from-log", str(ARCHIVE / name))
        assert rc == 0, f"{name}: {out}"
        assert "rounds parsed: 30 （已排除 1 個 b.N==1 的校準輪）" in out


# ---------------------------------------------------------------------------
# 2. The resolved divergences
# ---------------------------------------------------------------------------

def _all_calib(lines):
    return [re.sub(r"bench_n=\d+", "bench_n=1", l) if l.startswith("PROBEROW") else l
            for l in lines]


def _prefix_first_row(lines):
    out = list(lines)
    for i, l in enumerate(out):
        if l.startswith("PROBEROW"):
            out[i] = "BenchmarkProbeWriteLatency-4   \t" + l
            return out
    return out


def _prefix_first_env(lines):
    out = list(lines)
    for i, l in enumerate(out):
        if l.startswith("PROBEENV"):
            out[i] = "BenchmarkProbeWriteLatency-4   \t" + l
            return out
    return out


def _dup_first_env(lines):
    for i, l in enumerate(lines):
        if l.startswith("PROBEENV"):
            return lines[:i + 1] + [l] + lines[i + 1:]
    return list(lines)


def _only_two_measurement_rounds(lines):
    keep, seen = [], 0
    for l in lines:
        if l.startswith("PROBEROW"):
            seen += 1
            if seen > 3:          # 1 calibration + 2 measurement
                continue
        keep.append(l)
    return keep


def _constant_write_sum(lines):
    return [re.sub(r"write_sum=\d+", "write_sum=1000000", l)
            if l.startswith("PROBEROW") else l for l in lines]


def test_divergence1_ambiguous_calibration_is_stated_not_counted(tmp_path):
    """All rows b.N==1: the calibration round cannot be identified.

    ⛔ The archive copy printed "N measurement rounds, N calibration dropped"
    over the same N rows — a header that contradicts itself. Both renderers now
    say the rows were kept and why.
    """
    d = build_archive(tmp_path, _all_calib)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert "無法區分校準輪，全數保留" in out
    assert "已排除" not in out

    rc, out = run("--archive", str(d))
    assert rc == 0, out
    run1_block = out.split("PER DISPATCH", 1)[1].split("probe-run2.txt", 1)[0]
    assert "calibration indistinguishable, none dropped" in run1_block
    assert "calibration dropped" not in run1_block.replace(
        "calibration indistinguishable, none dropped", "")


def test_divergence2_records_are_found_when_go_test_prefixes_the_line(tmp_path):
    """`go test` prefixes the first line of each invocation; records must survive.

    ⛔ The archive copy anchored at line start and dropped such rows SILENTLY —
    and when the dropped row was the calibration round, the cold-start round got
    pooled into the measurements with no warning at all.
    """
    d = build_archive(tmp_path, _prefix_first_row)
    rc, out = run("--archive", str(d))
    assert rc == 0, out
    run1_block = out.split("PER DISPATCH", 1)[1].split("probe-run2.txt", 1)[0]
    assert "30 measurement rounds, 1 calibration dropped" in run1_block

    d2 = build_archive(tmp_path / "env", _prefix_first_env)
    rc, out = run("--archive", str(d2))
    assert rc == 0, out
    identity = out.split("probe-run1.txt", 1)[1].split("probe-run2.txt", 1)[0]
    assert identity.count("PROBEENV") == 2


def test_divergence3_correlations_need_a_floor_of_rounds(tmp_path):
    """Pearson over two points is ±1 by construction, in both renderers."""
    d = build_archive(tmp_path, _only_two_measurement_rounds)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert "不印相關係數" in out
    assert "corr(round total" not in out

    rc, out = run("--archive", str(d))
    assert rc == 0, out
    assert "correlations NOT printed" in out
    assert "corr(total, p50xN)" not in out


def test_divergence4_nan_rendering_is_deliberately_still_asymmetric(tmp_path):
    """⛔ Pins a KNOWN inconsistency, on purpose.

    Constant write_sum drives the correlation's denominator to zero. The CI
    renderer prints "+nan", the archive renderer prints "nan", and neither uses
    this report's own n/a convention. Unifying that is TRK-375 (#1733); it was
    kept out of #1731 so the de-duplication diff stayed readable.

    ⚠️ If #1733 unifies them, this test SHOULD go red — that is the signal that
    the decision recorded here has been revisited deliberately.
    """
    d = build_archive(tmp_path, _constant_write_sum)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert "| `write_sum` | +nan |" in out

    rc, out = run("--archive", str(d))
    assert rc == 0, out
    assert re.search(r"\bnan\b", out)
    assert "+nan" not in out


def test_divergence5_a_repeated_env_record_is_shown_not_swallowed(tmp_path):
    """A duplicate PROBEENV means two invocations of identical shape.

    ⛔ NOT one of the four divergences the issue listed — found by differential
    enumeration over constructed inputs while implementing the de-duplication.
    `-benchtime=Nx` does not produce two identical PROBEENV records, so seeing
    one means something merged two logs. The archive copy applied
    dict.fromkeys and hid it, in the section whose entire job is to say what was
    measured.
    """
    d = build_archive(tmp_path, _dup_first_env)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert out.count("PROBEENV goos=linux") == 3

    rc, out = run("--archive", str(d))
    assert rc == 0, out
    identity = out.split("probe-run1.txt", 1)[1].split("probe-run2.txt", 1)[0]
    assert identity.count("PROBEENV") == 3


# ---------------------------------------------------------------------------
# 3. Rejection paths — an ::error:: line and rc=2, never a half-printed report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mutate,needle", [
    (lambda ls: [l for l in ls if not l.startswith("PROBEENV")],
     "no PROBEENV record"),
    (lambda ls: [re.sub(r"iters=\d+", "iters=0", l)
                 if l.startswith("PROBEROW") and "bench_n=1" not in l else l
                 for l in ls],
     "iters<=0"),
])
def test_malformed_input_is_rejected_with_an_error_annotation(tmp_path, mutate, needle):
    d = build_archive(tmp_path, mutate)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 2, out
    assert "::error::" in out
    assert needle in out


def test_too_few_measurement_rounds_is_rejected(tmp_path):
    def one_round(lines):
        keep, seen = [], 0
        for l in lines:
            if l.startswith("PROBEROW"):
                seen += 1
                if seen > 2:
                    continue
            keep.append(l)
        return keep

    d = build_archive(tmp_path, one_round)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 2, out
    assert "::error::expected >=2 measurement PROBEROW records" in out


def test_missing_archive_directory_is_rejected():
    rc, out = run("--archive", str(REPO / "does-not-exist"))
    assert rc == 2, out
    assert "::error::missing data file" in out


# ---------------------------------------------------------------------------
# 3b. The archive file-list contract — restored after blind review
# ---------------------------------------------------------------------------
# ⛔ The tests in this block exist because an earlier draft globbed `probe-run*.txt` instead
# of using the fixed list the original had. That was a SIXTH behavioural
# divergence in a change that claimed five, and none of the tests above could
# see it: they all run against a directory holding exactly the three files.

def test_an_incomplete_archive_is_refused_not_summarised(tmp_path):
    """Two files present: the original refused; the glob draft printed a report."""
    d = build_archive(tmp_path, files=["probe-run1.txt", "probe-run2.txt"])
    rc, out = run("--archive", str(d))
    assert rc == 2, out
    assert "::error::missing data file" in out
    assert "probe-run3.txt" in out
    assert "CROSS DISPATCH" not in out


def test_a_fourth_run_file_does_not_move_the_cross_dispatch_numbers(tmp_path):
    """⚠️ Pins INHERITED behaviour, not a decision made here.

    The original ignored anything not named `probe-run{1,2,3}.txt`, so a stray
    fourth file is silently skipped rather than rejected. That is arguably the
    wrong default, but changing it is not this change's business. What must not
    happen is the glob draft's behaviour, where the extra file silently shifted
    every dispatch's percentage (probe-run1 read +5.34% -> +2.60% on identical
    data) under a heading still hard-coded to say "three".
    """
    d = build_archive(tmp_path)
    (d / "probe-run4.txt").write_text(
        (ARCHIVE / "probe-run1.txt").read_text(encoding="utf-8"), encoding="utf-8")
    rc, out = run_stdout("--archive", str(d))
    assert rc == 0, out
    expected = (FIXTURES / "archive.golden.txt").read_text(encoding="utf-8")
    assert out == expected, "a fourth file changed the report"


def test_the_two_modes_keep_their_own_too_few_rows_wording(tmp_path):
    """⛔ The two originals worded this refusal differently; both are output.

    An earlier draft unified them onto the workflow's phrasing, silently changing
    what a maintainer re-verifying the archive report sees.
    """
    def one_measurement_round(lines):
        keep, seen = [], 0
        for l in lines:
            if l.startswith("PROBEROW"):
                seen += 1
                if seen > 2:
                    continue
            keep.append(l)
        return keep

    d = build_archive(tmp_path, one_measurement_round)
    rc, out = run("--archive", str(d))
    assert rc == 2, out
    assert "::error::probe-run1.txt: parsed 1 measurement rows, need >= 2" in out

    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 2, out
    assert "::error::expected >=2 measurement PROBEROW records, parsed 1" in out


def test_from_log_refuses_a_missing_file_instead_of_raising(tmp_path):
    """⚠️ Pins a DELIBERATE improvement over the original, disclosed here.

    The workflow's inline copy had no existence check and died with a bare
    FileNotFoundError traceback (rc=1). The merged module gained the archive
    copy's guard on this path too, so it now refuses cleanly with rc=2. Net
    effect on the workflow step is the same (non-zero under `pipefail`), but the
    failure mode changed and that was not disclosed until blind review asked.
    """
    rc, out = run("--from-log", str(tmp_path / "nope.out"))
    assert rc == 2, out
    assert "::error::missing data file" in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# 3c. Branches that carry the longest design comments and had no test at all
# ---------------------------------------------------------------------------
# ⛔ Every test in this block was written because blind-review mutation testing
# showed the branch could be deleted or inverted with the whole suite staying
# green. The comments defending them were the only thing protecting them.

def test_the_correlation_floor_is_five_not_merely_present(tmp_path):
    """⛔ The VALUE was unpinned: lowering the floor 5 -> 3 kept every test green.

    Four measurement rounds sit between 3 and 5, so this fails if the floor is
    lowered to 3 or 4, which the two-round fixture below could never see.
    """
    def four_measurement_rounds(lines):
        keep, seen = [], 0
        for l in lines:
            if l.startswith("PROBEROW"):
                seen += 1
                if seen > 5:          # 1 calibration + 4 measurement
                    continue
            keep.append(l)
        return keep

    d = build_archive(tmp_path, four_measurement_rounds)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert "只有 4 輪，少於 5 輪" in out
    assert "corr(round total" not in out


def test_a_zero_denominator_prints_n_a_rather_than_killing_the_report(tmp_path):
    """⛔ `pct()`'s guard carries a ten-line defence and had no test.

    Removing the guard entirely left all tests green. A round whose `load_p50`
    and `write_sum` are zero drives `write_sum spread`'s divisor (the median of
    write_sum) to zero.
    """
    def zero_write(lines):
        return [re.sub(r"write_sum=\d+", "write_sum=0", l)
                if l.startswith("PROBEROW") else l for l in lines]

    d = build_archive(tmp_path, zero_write, mutate_all=True)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    assert "n/a（除數為 0）" in out
    assert "Traceback" not in out

    # ⚠️ The archive renderer needs a DIFFERENT construction: none of its
    # divisors is a write_sum median, so zeroing write_sum only zeroes
    # numerators there. Its zero-divisor is `stdev(round total)`, reached by
    # making every round numerically identical. Found by running it, not by
    # reading — the first version of this test asserted the archive half with
    # the CI half's input and failed for a reason unrelated to the guard.
    def identical_rounds(lines):
        out_lines = []
        for l in lines:
            if l.startswith("PROBEROW"):
                for k, v in (("write_sum", 1000000), ("load_sum", 2000000),
                             ("load_p50", 5000), ("write_p99", 100),
                             ("write_max", 200), ("load_p99", 6000),
                             ("load_max", 7000)):
                    l = re.sub(rf"{k}=\d+", f"{k}={v}", l)
            out_lines.append(l)
        return out_lines

    d2 = build_archive(tmp_path / "flat", identical_rounds, mutate_all=True)
    rc, out = run("--archive", str(d2))
    assert rc == 0, out
    assert "n/a (denominator is 0)" in out
    assert "Traceback" not in out


def test_the_null_observation_branches_render_in_both_modes(tmp_path):
    """⛔ All four `else` branches were dead to the suite.

    Constant write_sum makes `max(w) - median(w) == 0`, taking the "no positive
    excursion" branch; both renderers have one, and each is a whole sentence
    that must not be spliced with the other branch's preamble.
    """
    d = build_archive(tmp_path, _constant_write_sum, mutate_all=True)
    rc, out = run("--from-log", str(d / "probe-run1.txt"))
    assert rc == 0, out
    # ⛔ BOTH halves. The branch is two adjacent string literals and the first
    # version of this test asserted only the first — dogfood corrupted the
    # second half and stayed green, which is the same "assertion names the
    # branch but does not cover it" defect this whole block exists to remove.
    assert "沒有任何一輪的 `write_sum` 高於中位" in out
    assert "null 觀測，不是量測失敗" in out
    assert "高於中位最多的一輪是" not in out

    rc, out = run("--archive", str(d))
    assert rc == 0, out
    assert "no write_sum sat above the median" in out
    assert "Largest write-path excursion" not in out


def test_mismatched_run_shapes_warn_against_pooling(tmp_path):
    """⛔ `if len(shapes) != 1` could be turned off with every test still green.

    The warning is the only thing telling a reader that the CROSS DISPATCH
    section below it is comparing runs that are not comparable.
    """
    def other_arch(lines):
        return [l.replace("goarch=amd64", "goarch=arm64")
                if l.startswith("PROBEENV") else l for l in lines]

    d = build_archive(tmp_path, other_arch)
    rc, out = run("--archive", str(d))
    assert rc == 0, out
    assert "the runs are NOT all the same shape" in out


def test_archive_rejection_names_the_offending_file(tmp_path):
    """A three-file report must say WHICH file it refused, not just that it did."""
    d = build_archive(tmp_path, lambda ls: [l for l in ls if not l.startswith("PROBEENV")])
    rc, out = run("--archive", str(d))
    assert rc == 2, out
    assert "::error::probe-run1.txt:" in out


# ---------------------------------------------------------------------------
# 4. In-process entry — so coverage can see this module at all
# ---------------------------------------------------------------------------
# ⛔ Every other test here runs the tool as a subprocess — the right interface to
# assert, since that is what the workflow consumes, but invisible to coverage.py,
# which does not follow child processes. With only those, this module reports as
# "never imported" and every coverage-derived signal reads it as untested. These
# three give coverage something to see.
# ⚠️ Use the MODULE selector, not a path: `--cov=<path>` reports "never imported"
# even for in-process tests, which is a broken instrument rather than a result.
#
#     PYTHONPATH=scripts/tools/dx:scripts/tools python3 -m pytest \
#         tests/dx/test_analyze_probe.py -k in_process --cov=analyze_probe
#
# ⛔ No test counts or coverage percentages are quoted here. Every version of this
# comment that quoted one was wrong or went stale within the same change. Run the
# command.
#
# ⭐ They also carry detection the subprocess tests structurally cannot: making
# `main` run the renderer but return None instead of its value is caught only
# here, because `sys.exit(None)` exits 0 and a subprocess cannot tell that from
# success. The `main()` return contract is invisible to the other interface.
# ⚠️ The coverage number they produce is not evidence the tool is well tested —
# the subprocess tests carry most of the detection power.
# ⛔ The blind spot is repo-wide, not local to this file: many files under
# `tests/` invoke tools through `sys.executable` (count them with
# `grep -rl sys.executable tests --include='*.py'`). Whether each is also imported
# somewhere was NOT measured, so no count of affected modules is claimed here.
# Tracked as TRK-379 (#1746). ⚠️ It was numbered TRK-378 when first written; that
# number collided with #1741, which landed on main first, so this one was
# renumbered. See the TRK-379 row in planning-id-mapping.md for the arbitration.

def test_archive_mode_runs_in_process(capsys):
    """Same run as the --archive golden, but imported rather than spawned."""
    import analyze_probe

    assert analyze_probe.main(["--archive", str(ARCHIVE)]) == 0
    out = capsys.readouterr().out
    assert "IDENTITY OF THE THING MEASURED" in out
    assert "CROSS DISPATCH" in out


def test_ci_mode_runs_in_process(capsys):
    """Same run as the --from-log golden, but imported rather than spawned."""
    import analyze_probe

    assert analyze_probe.main(["--from-log", str(ARCHIVE / "probe-run1.txt")]) == 0
    out = capsys.readouterr().out
    assert "## Probe: write vs load latency (#1497 mechanism 1)" in out


def test_rejection_returns_two_in_process(tmp_path, capsys):
    """The refusal path returns rc=2 rather than raising — asserted in-process.

    The subprocess tests already pin the exit status; this one pins that `main`
    RETURNS it, which is what `sys.exit(main())` depends on.
    """
    import analyze_probe

    d = build_archive(tmp_path, lambda ls: [l for l in ls if not l.startswith("PROBEENV")])
    assert analyze_probe.main(["--from-log", str(d / "probe-run1.txt")]) == 2
    assert "::error::" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 5. The workflow actually calls this tool, with a path CI will resolve
# ---------------------------------------------------------------------------

WORKFLOW = REPO / ".github" / "workflows" / "bench-probe-write-latency.yaml"


def _workflow_doc():
    """Parse the workflow. ⛔ Parsed, not grepped — see the two tests below.

    The first version of both tests searched the file's RAW TEXT. Blind review
    showed each was satisfiable without the property it named:
      - commenting the path out (`# - 'scripts/...'`) left the substring in the
        file, so the path-filter test stayed green while the filter no longer
        matched; a `paths-ignore:` entry did the same;
      - the invocation test's needle also occurs in the path-filter list, so
        pointing the Summarize step at a DIFFERENT program kept it green.
    Parsing removes both evasions: a comment is not a list item, and the step's
    `run:` string is a different node from the trigger's `paths` list.
    """
    import yaml

    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _summarize_step(doc):
    """The step that turns probe.out into the job summary.

    ⚠️ Located by what it DOES (writes to `$GITHUB_STEP_SUMMARY`), not by an
    exact name. Blind review showed that requiring the name `Summarize` verbatim
    turned red on renaming the step to "Summarize the probe output" — a harmless
    edit that has nothing to do with what these tests protect.
    """
    steps = doc["jobs"]["probe"]["steps"]
    matches = [s for s in steps if "GITHUB_STEP_SUMMARY" in (s.get("run") or "")]
    assert len(matches) == 1, (
        f"expected exactly one step writing to GITHUB_STEP_SUMMARY, got "
        f"{len(matches)}: {[s.get('name') for s in matches]}"
    )
    return matches[0]


def test_workflow_invokes_this_tool_and_no_longer_inlines_the_computation():
    """⛔ The point of #1731 is that the workflow has no second copy.

    Asserted on the Summarize step's own `run:` script, so pointing it at some
    other program fails here rather than being satisfied by the path-filter
    entry elsewhere in the file.
    """
    run_script = _summarize_step(_workflow_doc())["run"]
    # ⛔ SHELL comments are stripped first. Parsing YAML removed the "a YAML
    # comment still contains the substring" evasion but not this one: a `#` line
    # INSIDE the `run:` block scalar is part of the run string, and this step
    # already carries comments naming the tool. Blind review pointed the step at
    # `other_tool.py`, added `# replaces scripts/tools/dx/analyze_probe.py`, and
    # both workflow tests stayed green. Assert on the executable lines only.
    code = "\n".join(
        line for line in run_script.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "scripts/tools/dx/analyze_probe.py" in code, (
        "the Summarize step's executable lines do not invoke this tool "
        f"(comments stripped); they are:\n{code}"
    )
    assert "--from-log" in code
    # ⛔ Any heredoc, not just the one tag the first version looked for: a pasted
    # copy under `<<'PY2'` evaded that check and stayed green.
    assert not re.search(r"<<-?\s*['\"]?\w+['\"]?\s*$", code, re.M), (
        "the Summarize step contains a heredoc — an inline copy of the "
        "computation may have been pasted back beside the tool call"
    )
    # ⚠️ Deliberately NOT `count("python3") == 1`: blind review showed that
    # reddened on a harmless `python3 --version` diagnostic line, which is not
    # what this test protects. The property is "no SECOND program computes the
    # summary", so only python3 lines that actually run something count — a line
    # naming a `.py` file, or `-c` inline code. `python3 --version` names
    # neither and is allowed.
    # ⚠️ This narrower predicate was itself dogfooded both ways: `python3
    # --version` must stay green, `python3 other_tool.py` must go red.
    others = []
    for line in code.splitlines():
        if "python3" not in line:
            continue
        if "scripts/tools/dx/analyze_probe.py" in line:
            continue
        # ⛔ `.py`, `-c` AND `-m`. The first version of this predicate checked
        # only `.py`/`-c`, so `python3 -m some_module >> "$GITHUB_STEP_SUMMARY"`
        # — a second program writing the summary, exactly what this test exists
        # to forbid — passed. `python3 --version` names none of the three and
        # stays allowed; both directions are pinned by the dogfood harness.
        if ".py" in line or re.search(r"python3\s+(-\w+\s+)*-[cm]\b", line):
            others.append(line.strip())
    assert not others, (
        f"the Summarize step runs python3 on something other than this tool: {others}"
    )


def test_workflow_self_test_would_run_on_a_change_to_this_tool():
    """⚠️ The pull_request trigger is path-filtered.

    Without this path in the filter the self-test silently stops covering the
    file that does all the work — the PR goes green having exercised nothing.
    """
    doc = _workflow_doc()
    # ⚠️ `on` is parsed by PyYAML 1.1 semantics as the boolean True, not "on".
    trigger = doc[True] if True in doc else doc["on"]
    pr = trigger["pull_request"]
    paths = pr.get("paths") or []
    assert "scripts/tools/dx/analyze_probe.py" in paths, (
        f"tool missing from pull_request.paths; filter is {paths}"
    )
    # ⛔ An ignore entry would cancel the include even though the include is
    # still listed — assert the trigger has no ignore list at all rather than
    # trying to reason about precedence.
    assert not pr.get("paths-ignore"), (
        f"pull_request.paths-ignore is set ({pr.get('paths-ignore')}) and can "
        "cancel the include above"
    )
