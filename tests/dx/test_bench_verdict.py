"""Tests for bench_verdict.py — the bench gate's single benchstat reader.

What these pin, and why each one matters:

  1. The UNIT of a flagged row is known. benchstat prints the unit only in the
     section header; a reader that greps rows alone cannot tell a +30%
     allocs/op row (the code does more work) from a +8% sec/op row on a noisy
     shared runner. Four post-merge attribution issues were the second kind.
  2. A timing-only flag is REPLICATED, not trusted and not dropped: it stands
     only for a benchmark a second independent pass also flags.
  3. Three states. A drifting, missing or heterogeneous canary — in EITHER
     pass — is inconclusive, never a quiet "clean".
  4. Unrecognised benchstat output exits 2. A comparison nobody could parse
     must never read as "no regression" (the #611 failure class).

Fixtures under fixtures/bench_verdict/ are REAL benchstat output
(golang.org/x/perf @ 2026-10-09) over synthetic `go test -bench` text with the
same shape bench_interleave.sh produces — six rounds, product benchmarks plus
the control-canary package.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import bench_verdict as bv

FIX = Path(__file__).parent / "fixtures" / "bench_verdict"
REPO = Path(__file__).resolve().parents[2]


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def reading(name: str) -> bv.Reading:
    return bv.parse(text(name))


# ── 1. unit-aware parsing ───────────────────────────────────────────────────

def test_each_flagged_row_carries_its_section_unit():
    r = reading("deterministic.txt")
    assert [(row.bench, row.unit) for row in r.regressions] == [
        ("Merge_1000-4", "sec/op"),
        ("Scan_1000-4", "allocs/op"),
    ]
    assert [row.bench for row in r.deterministic] == ["Scan_1000-4"]
    assert [row.bench for row in r.timing] == ["Merge_1000-4"]


def test_rows_are_reported_verbatim_for_issue_bodies():
    # PR comments and attribution issues paste these lines as-is.
    r = reading("timing_only.txt")
    assert r.regressions[0].line in text("timing_only.txt").splitlines()


def test_canaries_are_never_product_regressions():
    r = reading("canary_drift.txt")
    assert all(not row.bench.startswith("ControlCanary") for row in r.regressions)


def test_a_new_package_resets_the_unit():
    # A row after `pkg:` but before that package's unit header has no unit.
    broken = "pkg: x\nFoo-4   1.0m ± 1%   2.0m ± 1%  +100.00% (p=0.002 n=6)\n"
    body = text("clean.txt") + "\n" + broken
    with pytest.raises(bv.ShapeError, match="outside any unit section"):
        bv.parse(body)


# ── 2. replication ──────────────────────────────────────────────────────────

def test_deterministic_evidence_needs_no_second_pass():
    first = reading("deterministic.txt")
    assert bv.needs_replication(first) is False
    v = bv.decide(first, None)
    assert (v.regression, v.basis) == (True, "deterministic")
    # The timing row in the same run is corroborated, so it is kept.
    assert [r.bench for r in v.regressions] == ["Merge_1000-4", "Scan_1000-4"]


def test_timing_only_asks_for_a_second_pass():
    assert bv.needs_replication(reading("timing_only.txt")) is True


def test_timing_only_that_replicates_is_a_regression():
    v = bv.decide(reading("timing_only.txt"), reading("timing_only_again.txt"))
    assert (v.regression, v.basis) == (True, "timing-replicated")
    assert [r.bench for r in v.regressions] == ["Merge_1000-4"]
    assert v.unreplicated == []


def test_timing_only_that_does_not_replicate_is_reported_not_counted():
    v = bv.decide(reading("timing_only.txt"), reading("clean.txt"))
    assert (v.regression, v.basis) == (False, "timing-not-replicated")
    assert [r.bench for r in v.unreplicated] == ["Merge_1000-4"]
    assert v.inconclusive_reason == ""


def test_without_a_second_pass_timing_only_is_reported_as_before():
    # REPLICATE_TIMING_ONLY=0: the caller opted out; legacy behaviour.
    v = bv.decide(reading("timing_only.txt"), None)
    assert (v.regression, v.basis) == (True, "timing-unreplicated")


def test_clean_run():
    first = reading("clean.txt")
    assert bv.needs_replication(first) is False
    v = bv.decide(first, None)
    assert (v.regression, v.basis, v.inconclusive_reason) == (False, "clean", "")


# ── 3. three states ─────────────────────────────────────────────────────────

def test_canary_drift_is_inconclusive_and_never_replicated():
    first = reading("canary_drift.txt")
    assert "drifted" in first.inconclusive_reason()
    assert bv.needs_replication(first) is False
    v = bv.decide(first, None)
    assert v.basis == "inconclusive"
    assert any("ControlCanaryCPU" in line for line in v.canary_rows)


def _without_canary(body: str) -> str:
    return body.split("pkg: example.com/canary")[0]


def test_missing_canary_is_inconclusive():
    r = bv.parse(_without_canary(text("clean.txt")))
    assert "absent" in r.inconclusive_reason()


def test_heterogeneous_cpus_are_inconclusive():
    body = text("clean.txt").replace(
        "pkg: example.com/canary", "pkg: example.com/canary\ncpu: Some Other CPU", 1)
    r = bv.parse(body)
    assert "Heterogeneous" in r.inconclusive_reason()
    out = bv.render_outputs(bv.decide(r, None), "b.txt")
    assert "inconclusive_cpus<<" in out


def test_an_unstable_second_pass_cannot_dismiss_the_first():
    v = bv.decide(reading("timing_only.txt"), reading("canary_drift.txt"))
    assert v.basis == "inconclusive"
    assert v.regression is False
    assert v.inconclusive_reason.startswith("Replication pass:")
    assert [r.bench for r in v.unreplicated] == ["Merge_1000-4"]


# ── 4. fail loud ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("body, needle", [
    ("", "metric-section header"),
    ("goos: linux\n│ sec/op │\nFoo-4 1 2 ~\n", "per-bench result row"),
])
def test_unrecognised_shape_raises(body, needle):
    with pytest.raises(bv.ShapeError, match=needle):
        bv.parse(body)


def test_cli_exits_2_on_unparseable_input(tmp_path, capsys):
    bad = tmp_path / "b.txt"
    bad.write_text("not benchstat\n", encoding="utf-8")
    out = tmp_path / "out"
    rc = bv.main(["verdict", "--benchstat", str(bad), "--github-output", str(out)])
    assert rc == 2
    assert not out.exists()  # nothing that could read as a verdict
    assert "::error::" in capsys.readouterr().err


def test_cli_exits_2_on_missing_file(tmp_path):
    assert bv.main(["needs-replication", "--benchstat", str(tmp_path / "nope")]) == 2


# ── CLI / output contract ───────────────────────────────────────────────────

def test_cli_needs_replication_prints_one_word(capsys):
    assert bv.main(["needs-replication", "--benchstat", str(FIX / "timing_only.txt")]) == 0
    assert capsys.readouterr().out == "yes\n"
    assert bv.main(["needs-replication", "--benchstat", str(FIX / "deterministic.txt")]) == 0
    assert capsys.readouterr().out == "no\n"


def _outputs(path: Path) -> dict[str, str]:
    """Parse a GITHUB_OUTPUT file the way the runner does."""
    out: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line:
            key, delim = line.split("<<", 1)
            body = []
            i += 1
            while lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key] = "\n".join(body)
        else:
            key, _, val = line.partition("=")
            out[key] = val
        i += 1
    return out


def test_cli_writes_the_documented_step_outputs(tmp_path):
    out = tmp_path / "gh_out"
    rc = bv.main(["verdict", "--benchstat", str(FIX / "timing_only.txt"),
                  "--replicate", str(FIX / "clean.txt"), "--github-output", str(out)])
    assert rc == 0
    o = _outputs(out)
    assert o["regression"] == "false"
    assert o["inconclusive"] == "false"
    assert o["regression_basis"] == "timing-not-replicated"
    assert "Merge_1000-4" in o["unreplicated"]
    assert o["benchstat_txt"] == str(FIX / "timing_only.txt")
    assert "regressions" not in o


def test_cli_regression_outputs(tmp_path):
    out = tmp_path / "gh_out"
    assert bv.main(["verdict", "--benchstat", str(FIX / "deterministic.txt"),
                    "--github-output", str(out)]) == 0
    o = _outputs(out)
    assert o["regression"] == "true"
    assert o["regression_basis"] == "deterministic"
    assert o["regressions"].splitlines() == [
        r.line for r in reading("deterministic.txt").regressions]


# ── single reader ───────────────────────────────────────────────────────────

def test_the_shell_core_does_not_parse_benchstat_itself():
    """bench_gate_compare.sh orchestrates; bench_verdict.py is the only reader.

    A second parser in the shell is how the three consumers' verdicts would
    drift apart again — the reason the core was extracted in the first place.
    """
    sh = (REPO / "scripts/tools/ops/bench_gate_compare.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in sh.splitlines() if not l.lstrip().startswith("#"))
    assert "bench_verdict.py\" verdict" in code or "$VERDICT\" verdict" in code
    for parser_shape in ("awk", "ControlCanaryCPU", "(p="):
        assert parser_shape not in code, parser_shape
