"""Tests for analyze_probe.py — the de-duplicated probe summary (TRK-373 / #1731).

⭐ What this file is actually protecting, and why it is shaped this way.

Until #1731 this computation existed TWICE: inline in the Summarize step of
.github/workflows/bench-probe-write-latency.yaml, and in
docs/internal/audit-reports/bench-probe-2026-09/analyze_probe.py. Nothing checked
that the two agreed and five behavioural divergences had accumulated.

⛔ The fix was to delete one copy, NOT to add a test that compares two copies —
a differential test would have locked the duplication in place (every future edit
would then need two edits plus a test edit). So there is deliberately NO
copy-vs-copy assertion here.

What is pinned instead:

  1. GOLDEN — both renderers, byte for byte, against output captured from the
     PRE-CHANGE code. This is the only artefact that can still answer "did the
     de-duplication change what the reports say", because the copy it was
     captured from no longer exists in the tree.
     ⚠️ Regenerating a golden file is how you SILENCE this test. If a change is
     meant to alter a report, say so in the commit message and regenerate
     deliberately; do not regenerate to make a red test go away.

  2. THE FIVE RESOLVED DIVERGENCES — each as the behaviour that was CHOSEN, on a
     constructed input that used to tell the two copies apart. These are the
     tests with detection power for a regression back to the old behaviour: the
     golden files cannot see any of them, because all five needed a degenerate
     input and the archived probe-run*.txt are all well-formed.

  3. THE REJECTION PATHS — rc=2 plus an ::error:: line, not a traceback.
     ⚠️ Only the three that already rejected. Malformed records (a field with no
     "=", a non-numeric value) still raise a bare traceback in BOTH renderers;
     that is TRK-375 (#1733) and is deliberately NOT fixed here. The tests below
     pin the CURRENT behaviour so that #1733 has to change them on purpose.

⛔ Coverage limit, stated because the alternative is implying it does not exist:
divergence 4 (NaN rendering) is pinned as "still different on the two sides" —
that non-unification was a deliberate scope decision, not an oversight, and
#1733 owns it.
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
    """
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(TOOL), *args],
        capture_output=True, text=True, cwd=cwd, timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def build_archive(tmp_path, mutate=None, files=RUNS):
    """Copy the real archive into tmp_path, optionally mutating probe-run1.txt."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    for name in files:
        lines = (ARCHIVE / name).read_text(encoding="utf-8").splitlines()
        if mutate is not None and name == "probe-run1.txt":
            lines = mutate(lines)
        (tmp_path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Golden — the de-duplication must not have changed either report
# ---------------------------------------------------------------------------

def test_archive_report_matches_the_pre_change_golden():
    rc, out = run("--archive", str(ARCHIVE))
    assert rc == 0, out
    expected = (FIXTURES / "archive.golden.txt").read_text(encoding="utf-8")
    assert out == expected


def test_ci_summary_matches_the_pre_change_golden():
    rc, out = run("--from-log", str(ARCHIVE / "probe-run1.txt"))
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
# 2. The five resolved divergences
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
    assert "no probe-run*.txt found" in out


def test_archive_rejection_names_the_offending_file(tmp_path):
    """A three-file report must say WHICH file it refused, not just that it did."""
    d = build_archive(tmp_path, lambda ls: [l for l in ls if not l.startswith("PROBEENV")])
    rc, out = run("--archive", str(d))
    assert rc == 2, out
    assert "::error::probe-run1.txt:" in out


# ---------------------------------------------------------------------------
# 4. The workflow actually calls this tool, with a path CI will resolve
# ---------------------------------------------------------------------------

WORKFLOW = REPO / ".github" / "workflows" / "bench-probe-write-latency.yaml"


def test_workflow_invokes_this_tool_and_no_longer_inlines_the_computation():
    """⛔ The point of #1731 is that the workflow has no second copy.

    Asserting the call alone would still pass if an inline copy were pasted back
    beside it, so the absence of the heredoc is asserted too.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/tools/dx/analyze_probe.py" in text
    assert "--from-log" in text
    assert "<<'PY'" not in text


def test_workflow_self_test_would_run_on_a_change_to_this_tool():
    """⚠️ The pull_request trigger is path-filtered.

    Without this path in the filter the self-test silently stops covering the
    file that does all the work — the PR goes green having exercised nothing.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    trigger = text.split("pull_request:", 1)[1].split("permissions:", 1)[0]
    assert "scripts/tools/dx/analyze_probe.py" in trigger
