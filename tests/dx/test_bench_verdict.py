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


def test_a_change_from_zero_is_a_deterministic_regression():
    # benchstat prints "? (p=…)" when the base is 0 (no percentage exists).
    # A path that did not allocate and now does is the clearest deterministic
    # regression there is; reading "?" as "no finding" would call it clean.
    r = reading("from_zero.txt")
    assert [(row.bench, row.unit) for row in r.regressions] == [
        ("Hot-4", "B/op"), ("Hot-4", "allocs/op")]
    assert all(row.delta_pct == float("inf") for row in r.regressions)
    v = bv.decide(r, None)
    assert (v.regression, v.basis) == (True, "deterministic")


def test_a_change_to_zero_is_an_improvement():
    r = reading("from_zero.txt")
    assert "Drop-4" not in {row.bench for row in r.regressions}


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


# ── acceptance ledger (ADR-039) ─────────────────────────────────────────────

LEDGER_OK = '''
[[accepted]]
benches = ["BenchmarkScan_1000"]
metric = "allocs/op"
max_delta_pct = 35
reason = "deliberate"
'''


def test_ledger_parses_a_valid_entry():
    (e,) = bv.parse_ledger(LEDGER_OK)
    assert (e.benches, e.metric, e.max_delta_pct, e.issue) == (
        ("BenchmarkScan_1000",), "allocs/op", 35.0, None)


def test_an_empty_ledger_is_valid():
    assert bv.parse_ledger("# only comments\n") == []


@pytest.mark.parametrize("body, needle", [
    ('[[accepted]]\nbenches=["Scan.*"]\nmetric="B/op"\nmax_delta_pct=5\nreason="r"\n', "not patterns"),
    ('[[accepted]]\nbenches=[]\nmetric="B/op"\nmax_delta_pct=5\nreason="r"\n', "non-empty list"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="ns/op"\nmax_delta_pct=5\nreason="r"\n', "metric"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="B/op"\nmax_delta_pct=0\nreason="r"\n', "> 0"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="B/op"\nmax_delta_pct=true\nreason="r"\n', "> 0"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="B/op"\nmax_delta_pct=5\nreason=" "\n', "reason"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="B/op"\nmax_delta_pct=5\n', "missing"),
    ('[[accepted]]\nbenches=["BenchmarkA"]\nmetric="B/op"\nmax_delta_pct=5\nreason="r"\nsha="x"\n', "unknown"),
    ('accepted = 1\n', "array of tables"),
    ('[other]\n', "top-level"),
    ('[[accepted]\n', "TOML"),
])
def test_ledger_rejects_malformed_entries(body, needle):
    with pytest.raises(bv.LedgerError, match=needle):
        bv.parse_ledger(body)


def test_a_covered_deterministic_row_is_accepted_not_counted():
    r = reading("deterministic.txt")  # Scan allocs +30%, Merge sec/op +7.10%
    accepted, over = bv.apply_ledger(r, bv.parse_ledger(LEDGER_OK))
    assert [j.row.bench for j in accepted] == ["Scan_1000-4"]
    assert over == []
    # Only the timing row remains → it now needs replication, like any other.
    assert [row.bench for row in r.regressions] == ["Merge_1000-4"]
    assert bv.needs_replication(r) is True


def test_beyond_the_declared_bound_is_still_a_regression():
    tight = LEDGER_OK.replace("max_delta_pct = 35", "max_delta_pct = 10")
    r = reading("deterministic.txt")
    accepted, over = bv.apply_ledger(r, bv.parse_ledger(tight))
    assert accepted == []
    assert [j.row.bench for j in over] == ["Scan_1000-4"]
    assert bv.decide(r, None).basis == "deterministic"


def test_the_metric_must_match_not_just_the_name():
    wrong_unit = LEDGER_OK.replace('"allocs/op"', '"B/op"')
    r = reading("deterministic.txt")
    accepted, _ = bv.apply_ledger(r, bv.parse_ledger(wrong_unit))
    assert accepted == []


def test_an_unbounded_from_zero_row_is_never_accepted():
    body = ('[[accepted]]\nbenches=["BenchmarkHot"]\nmetric="B/op"\n'
            'max_delta_pct=1000000\nreason="r"\n')
    r = reading("from_zero.txt")
    accepted, over = bv.apply_ledger(r, bv.parse_ledger(body))
    assert accepted == [] and [j.row.unit for j in over] == ["B/op"]


def _git(repo: Path, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8", timeout=30).stdout.strip()


@pytest.fixture
def ledger_repo(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / ".github").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "."), _git(repo, "commit", "-qm", "base")
    monkeypatch.chdir(repo)

    def commit(ledger: str | None) -> str:
        path = repo / bv.LEDGER_PATH
        if ledger is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(ledger, encoding="utf-8")
        _git(repo, "add", "-A"), _git(repo, "commit", "-qm", "c", "--allow-empty")
        return _git(repo, "rev-parse", "HEAD")

    return _git(repo, "rev-parse", "HEAD"), commit


def test_cli_applies_entries_added_in_the_range(ledger_repo, tmp_path):
    base, commit = ledger_repo
    head = commit(LEDGER_OK)
    out = tmp_path / "gh_out"
    assert bv.main(["verdict", "--benchstat", str(FIX / "deterministic.txt"),
                    "--replicate", str(FIX / "clean.txt"),
                    "--ledger-base", base, "--ledger-head", head,
                    "--github-output", str(out)]) == 0
    o = _outputs(out)
    # Scan's allocs are accepted; Merge's sec/op flag did not replicate.
    assert o["regression"] == "false"
    assert o["regression_basis"] == "timing-not-replicated"
    assert "Scan_1000-4" in o["accepted"] and "declared ≤ +35%" in o["accepted"]


def test_cli_reports_accepted_basis_when_nothing_else_remains(ledger_repo, tmp_path):
    base, commit = ledger_repo
    both = LEDGER_OK + LEDGER_OK.replace("BenchmarkScan_1000", "BenchmarkMerge_1000").replace(
        '"allocs/op"', '"sec/op"')
    head = commit(both)
    out = tmp_path / "gh_out"
    assert bv.main(["verdict", "--benchstat", str(FIX / "deterministic.txt"),
                    "--ledger-base", base, "--ledger-head", head,
                    "--github-output", str(out)]) == 0
    o = _outputs(out)
    assert (o["regression"], o["regression_basis"]) == ("false", "accepted")


def test_an_entry_already_on_the_base_does_not_apply(ledger_repo, tmp_path):
    _, commit = ledger_repo
    base = commit(LEDGER_OK)
    # Reformat + reorder keys: same entry, different text. Must not re-accept.
    head = commit('[[accepted]]\nreason = "deliberate"\nmax_delta_pct = 35.0\n'
                  'metric = "allocs/op"\nbenches = [ "BenchmarkScan_1000" ]\n')
    assert bv.added_entries(base, head) == []
    out = tmp_path / "gh_out"
    bv.main(["verdict", "--benchstat", str(FIX / "deterministic.txt"),
             "--ledger-base", base, "--ledger-head", head, "--github-output", str(out)])
    assert _outputs(out)["regression_basis"] == "deterministic"


def test_a_missing_ledger_file_means_no_entries(ledger_repo):
    base, commit = ledger_repo
    head = commit(None)
    assert bv.added_entries(base, head) == []


def test_a_broken_ledger_fails_loud(ledger_repo, tmp_path, capsys):
    base, commit = ledger_repo
    head = commit('[[accepted]]\nbenches=["Scan.*"]\n')
    out = tmp_path / "gh_out"
    rc = bv.main(["verdict", "--benchstat", str(FIX / "clean.txt"),
                  "--ledger-base", base, "--ledger-head", head, "--github-output", str(out)])
    assert rc == 2 and not out.exists()
    assert "::error::cannot apply" in capsys.readouterr().err


def test_ledger_flags_go_together(capsys):
    assert bv.main(["needs-replication", "--benchstat", str(FIX / "clean.txt"),
                    "--ledger-base", "HEAD"]) == 2


# ── the real ledger ─────────────────────────────────────────────────────────

def _benchmarks_in_tree() -> set[str]:
    import re
    names: set[str] = set()
    for p in (REPO / "components/threshold-exporter/app").rglob("*_test.go"):
        names.update(re.findall(r"^func (Benchmark\w+)\(", p.read_text(encoding="utf-8"),
                                re.MULTILINE))
    return names


def test_the_repo_ledger_parses_and_names_only_existing_benchmarks():
    """A renamed or removed benchmark must not leave an entry that silently
    matches nothing — or matches a future benchmark that reuses the name."""
    known = _benchmarks_in_tree()
    assert len(known) > 20, "benchmark scan found almost nothing — scanner broken"
    entries = bv.parse_ledger((REPO / bv.LEDGER_PATH).read_text(encoding="utf-8"))
    assert bv.lint_ledger(entries, known) == []


def test_lint_catches_a_name_no_benchmark_carries():
    entries = bv.parse_ledger(LEDGER_OK)
    assert bv.lint_ledger(entries, {"BenchmarkOther"}) == [
        "accepted entry #1: no benchmark named BenchmarkScan_1000"]
