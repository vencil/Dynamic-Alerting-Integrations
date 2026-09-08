"""Runs `bench-probe-2026-09/counterfactual.py` — the thing nothing was running.

⛔ WHY THIS FILE EXISTS. The harness validates the level/above-p50 discriminant
by scraping numbers out of what `scripts/tools/dx/analyze_probe.py` PRINTS. That
makes it rot the moment the report's shape changes — and until this file, nothing
ran it, so it would have rotted unnoticed. A verification nobody executes is the
state the harness was written to end, one layer up.

⛔ rc 1 AND rc 3 ARE DIFFERENT THINGS AND THIS FILE KEEPS THEM DIFFERENT.
`rc 1` = the harness ran and the discriminant misbehaved. `rc 3` = the harness
could not measure (the report was unreadable, the tool would not import, a cell
was missing). Asserting only "returncode == 0" would collapse both into "test
failed" and throw away the one distinction the harness exists to maintain — so
the failure messages here name which happened.

⛔ THE FIRST TEST ALONE WOULD BE A GREEN NOTHING CAN TURN RED. The other two are
its counterfactual: one proves a broken discriminant comes out as rc 1, the other
proves an unreadable report comes out as rc 3. Without them, "this test detects
regressions in the discriminant" would be a claim rather than a measurement.

⚠️ The harness is driven as a SUBPROCESS, matching `test_analyze_probe.py`'s own
reasoning: what a reader runs is a command, so a command is what gets asserted.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "scripts" / "tools" / "dx" / "analyze_probe.py"
LIB = REPO / "scripts" / "tools" / "_lib_compat.py"
HARNESS = (REPO / "docs" / "internal" / "audit-reports" / "bench-probe-2026-09"
           / "counterfactual.py")

PASS, CHECK_FAILED, CANNOT_MEASURE = 0, 1, 3


def run_harness(tool: Path | None = None):
    args = [sys.executable, "-B", str(HARNESS)]
    if tool is not None:
        args += ["--tool", str(tool)]
    p = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def _verdict(rc: int) -> str:
    return {PASS: "all checks passed", CHECK_FAILED: "a check FAILED",
            CANNOT_MEASURE: "COULD NOT MEASURE"}.get(rc, f"undocumented rc {rc}")


def copy_tool(tmp_path: Path, mutate=None) -> Path:
    """A copy of the tool in a layout it can import its sibling from.

    ⚠️ `analyze_probe.py` inserts its own parent directory on `sys.path` and
    imports `_lib_compat` from there, so a bare copy is unimportable — which the
    harness reports as rc 3, i.e. a mutation test written that way would pass for
    the wrong reason.
    """
    (tmp_path / "dx").mkdir()
    shutil.copy(LIB, tmp_path / "_lib_compat.py")
    src = TOOL.read_text(encoding="utf-8")
    if mutate is not None:
        old, new = mutate
        assert src.count(old) == 1, f"anchor appears {src.count(old)}x, not once"
        src = src.replace(old, new)
    out = tmp_path / "dx" / "analyze_probe.py"
    out.write_text(src, encoding="utf-8")
    return out


def test_the_harness_still_passes_against_the_shipped_tool():
    rc, out = run_harness()
    assert rc == PASS, (
        f"the counterfactual harness came back {_verdict(rc)} (rc {rc}).\n"
        "⛔ rc 1 means the discriminant in analyze_probe.py changed behaviour;\n"
        "⛔ rc 3 means the harness could not read the report at all — usually\n"
        "   because render_ci's table changed shape. They are different repairs.\n"
        f"{out}")


def test_a_broken_discriminant_comes_back_as_a_failed_check(tmp_path):
    """⛔ The counterfactual for the test above. Without it that test is a claim."""
    tool = copy_tool(tmp_path, (
        'def above(r):\n    return r["load_sum"] - r["load_p50"] * r["iters"]',
        'def above(r):\n    return r["load_sum"]'))
    rc, out = run_harness(tool)
    assert rc == CHECK_FAILED, (
        f"breaking above() should be a FAILED CHECK, got {_verdict(rc)}.\n{out}")
    assert "COULD NOT MEASURE" not in out


def test_an_unreadable_report_comes_back_as_could_not_measure(tmp_path):
    """The other half: the harness must not report unreadable as a failed check."""
    tool = copy_tool(tmp_path, ('("水位以上的質量", abv)', '("水位以上的質量", zzz)'))
    rc, out = run_harness(tool)
    assert rc == CANNOT_MEASURE, (
        f"an unreadable table should be COULD NOT MEASURE, got {_verdict(rc)}.\n{out}")
    assert "COULD NOT MEASURE" in out


@pytest.mark.parametrize("rc", [PASS, CHECK_FAILED, CANNOT_MEASURE])
def test_every_exit_code_this_file_asserts_on_has_a_name(rc):
    """⛔ A guard on the guard: an undocumented rc must not read as a known one."""
    assert not _verdict(rc).startswith("undocumented")
