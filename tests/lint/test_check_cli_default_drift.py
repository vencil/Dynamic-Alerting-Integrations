#!/usr/bin/env python3
"""Tests for check_cli_default_drift (#1556).

⛔ The load-bearing tests here are the ones that PLANT a defect. "the repo is
clean" is satisfied by any change that scans less — a narrower doc list, a
header shape that stops being recognised, a literal test that starts returning
None. Every such change produces the same green as a genuinely clean tree.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_cli_default_drift.py"


def _load():
    sys.path.insert(0, str(SCRIPT.parent))
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
    spec = importlib.util.spec_from_file_location("check_cli_default_drift", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


mod = _load()

_FAKE_DEFAULTS = {
    "widget": {"--rounds": 10, "--ratio": 0.001, "--dir": "conf.d",
               "--flagless": False, "--from-env": None},
}


def _doc(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake-cli-reference.md"
    p.write_text("#### widget\n\n**選項**\n\n"
                 "| 選項 | 說明 | 預設值 |\n|------|------|--------|\n" + body,
                 encoding="utf-8")
    return p


class TestPlantedDefects:
    """The detector must actually detect."""

    @pytest.mark.parametrize("row,flag", [
        ("| `--rounds <N>` | rounds | `0` |\n", "--rounds"),
        ("| `--ratio <R>` | ratio | `5` |\n", "--ratio"),
        ("| `--dir <PATH>` | dir | `elsewhere` |\n", "--dir"),
        ("| `--flagless` | toggle | true |\n", "--flagless"),
    ])
    def test_a_wrong_default_is_reported(self, tmp_path, row, flag):
        findings, stats, _, _bh = mod.scan((_doc(tmp_path, row),), _FAKE_DEFAULTS)
        assert [f.flag for f in findings] == [flag], (
            f"planted a wrong default for {flag} and the checker did not "
            f"report it (stats={stats}). A checker that cannot fail on a "
            f"known-bad input proves nothing about the real tree.")

    @pytest.mark.parametrize("row", [
        "| `--rounds <N>` | rounds | `10` |\n",
        "| `--ratio <R>` | ratio | `0.001` |\n",
        "| `--dir <PATH>` | dir | `./conf.d/` |\n",   # ./ and / are presentation
        "| `--flagless` | toggle | false |\n",
    ])
    def test_a_correct_default_is_not_reported(self, tmp_path, row):
        findings, stats, _, _bh = mod.scan((_doc(tmp_path, row),), _FAKE_DEFAULTS)
        assert findings == [], (
            f"false positive on a correct row: {findings}. A checker that "
            f"cries wolf gets its scan surface narrowed by the next person "
            f"who needs to ship.")
        assert stats["scored"] == 1


class TestNotScoredIsDisclosedNotCounted:

    @pytest.mark.parametrize("cell", ["（全部）", "stdout", "(interactive prompt)",
                                      "-", "—", "`$PROMETHEUS_URL` 或 `x`"])
    def test_prose_cells_are_counted_not_silently_passed(self, tmp_path, cell):
        row = f"| `--dir <PATH>` | dir | {cell} |\n"
        findings, stats, _, _bh = mod.scan((_doc(tmp_path, row),), _FAKE_DEFAULTS)
        assert findings == []
        assert stats["scored"] == 0, "a prose cell must not be scored"
        assert stats["doc_prose"] == 1, (
            "a prose cell must land in doc_prose so the report can say how "
            "much it did NOT look at; silently dropping it turns 'cannot see' "
            "into 'checked'.")

    def test_env_dependent_default_is_not_scored(self, tmp_path):
        row = "| `--from-env <URL>` | url | `http://x` |\n"
        findings, stats, _, _bh = mod.scan((_doc(tmp_path, row),), _FAKE_DEFAULTS)
        assert findings == []
        assert stats["actual_not_literal"] == 1


class TestUnknownHeaderFailsLoud:

    def test_a_flag_row_under_an_unmapped_header_is_an_error(self, tmp_path):
        p = tmp_path / "fake.md"
        p.write_text("#### widget\n\n| 旗標 | 敘述 | 出廠值 |\n"
                     "|------|------|--------|\n"
                     "| `--rounds <N>` | rounds | `0` |\n", encoding="utf-8")
        findings, _stats, notes, _bh = mod.scan((p,), _FAKE_DEFAULTS)
        assert findings == []
        hard = [n for n in notes if not n.startswith("unscoreable:")]
        assert hard, (
            "a flag row under a header this checker does not recognise was "
            "skipped silently. Renaming a table header would then remove that "
            "table from the scan with nothing going red anywhere.")
        assert "旗標" in hard[0]

    def test_the_known_headers_are_not_all_the_same_column(self):
        """The pin exists because the columns are not in a fixed order."""
        cols = set(mod._HEADER_DEFAULT_COL.values())
        assert cols == {1, 2}, (
            "if every mapped header put the default in the same column the "
            "header pin would be redundant; it is not — `| Flag | 預設 | 說明 |` "
            "puts it in the middle. Positional parsing reads the description "
            "as the default there.")


class TestRealRepo:

    def test_the_shipped_docs_have_no_comparable_drift(self):
        findings, stats, notes, by_header = mod.scan()
        assert findings == [], "\n".join(
            f"{f.doc}:{f.line} {f.command} {f.flag}: doc {f.documented} "
            f"vs argparse {f.actual}" for f in findings)
        # ⛔ Floor on rows this check can actually falsify, not on rows read.
        # Reading more rows while scoring none is exactly the failure mode.
        assert stats["scored"] >= 100, (
            f"only {stats['scored']} rows are comparable. A collapse here "
            f"means the checker stopped understanding the tables, not that "
            f"the tables got simpler.\n"
            f"⚠️ This floor has a large cushion by design and CANNOT see a "
            f"partial collapse — blind review removed 8 subcommands (44% of "
            f"the comparable rows) and stayed above it. What sees that is the "
            f"accounting identity in `_scan_surface_faults`, which is inside "
            f"the check and therefore reaches CI. This floor only catches a "
            f"near-total one.")
        assert stats["matched"] == stats["scored"]
        # The three binary-wrapper dispatchers own their flags in Go.
        unscoreable = {n.split()[1] for n in notes if n.startswith("unscoreable:")}
        assert unscoreable == {"guard", "parser", "batch-pr"}, (
            f"the set of subcommands this checker cannot introspect changed: "
            f"{sorted(unscoreable)}. If a subcommand joined that set because "
            f"its import broke, every one of its documented defaults left the "
            f"scan surface — and the check would still be green.")

    def test_the_set_of_productive_header_shapes_is_pinned(self):
        """Which header shapes actually yield comparisons — pinned, not floored.

        ⛔ Measured: flipping the default-column index of one header shape from
        1 to 2 left all 19 tests green. Those rows simply started reading the
        description cell, landed in `doc_prose`, and the global scored count
        stayed above any floor. A total cannot see one shape going blind; the
        set can.

        ⚠️ Three mapped shapes appear in the docs and legitimately score zero,
        so they are NOT in this set and a column-index error in them would go
        unnoticed here (disclosed rather than papered over):
          ('Flag','預設','說明')   — `guard` tables; flags owned by the Go binary
          ('Flag','Default','Purpose') — `batch-pr` tables, same reason
          ('參數','說明','預設')    — `init`, whose argparse defaults are None
        """
        _f, _st, _n, by_header = mod.scan()
        productive = {k for k, v in by_header.items() if v > 0}
        assert productive == {
            ("選項", "說明", "預設值"),
            ("Option", "Description", "Default"),
            ("參數", "說明", "預設值"),
            ("Parameter", "Description", "Default"),
            ("Flag", "Default", "說明"),
            ("Flag", "Default", "Description"),
            ("參數", "用途", "預設"),
            ("Argument", "Description", "Default"),
            ("Parameter", "Purpose", "Default"),
        }, (
            f"the set of header shapes yielding comparisons changed: "
            f"{sorted(productive)}. A shape dropping out means its rows are "
            f"being read from the wrong column or stopped being recognised — "
            f"both are indistinguishable from 'those tables have no drift'.")

    def test_no_dead_entries_in_the_header_pin(self):
        """A mapped header that appears nowhere is a stale rule nobody notices."""
        seen = set()
        for doc in mod.DOCS:
            for raw in doc.read_text(encoding="utf-8").splitlines():
                if raw.startswith("|"):
                    key = tuple(c.strip("* ") for c in mod._cells(raw))
                    if key in mod._HEADER_DEFAULT_COL:
                        seen.add(key)
        dead = sorted(set(mod._HEADER_DEFAULT_COL) - seen)
        assert not dead, (
            f"these mapped headers appear in no doc: {dead}. Either the tables "
            f"were renamed (and their rows are now unmapped) or the entries "
            f"are speculative.")


class TestEnvironmentIsScrubbed:

    def test_result_does_not_depend_on_the_ambient_environment(self):
        """Same tree, two environments, one answer.

        ⛔ Measured before the scrub: exporting PROMETHEUS_URL changes the
        captured default of 14 flags across 12 subcommands, so this check would
        go red on a runner that has it set with no doc change at all.
        """
        def run(extra_env):
            env = {**os.environ, **extra_env, "PYTHONIOENCODING": "utf-8"}
            return subprocess.run(
                [sys.executable, "-s", str(SCRIPT), "--json"],
                capture_output=True, timeout=600, env=env, cwd=str(REPO_ROOT))

        clean = run({})
        polluted = run({"PROMETHEUS_URL": "http://sentinel.invalid:9090",
                        "TENANT_API_URL": "http://sentinel.invalid",
                        "DA_GOVERN_GROUPS": "sentinel"})
        assert clean.returncode == polluted.returncode
        assert clean.stdout == polluted.stdout, (
            "the verdict changed when unrelated environment variables were "
            "set, so it is a property of the machine rather than of the CLI.")
        assert b"sentinel.invalid" not in polluted.stdout, (
            "an environment value reached the captured defaults; the scrub in "
            "introspect_defaults is not covering it.")


class TestBlindIsNotClean:
    """A check that cannot LOAD the tools must not report that it found nothing.

    ⛔ Counterfactual, measured on this branch before the split: with no command
    loading, ``scan()`` returned 0 findings / ``scored 0`` and ``main()``
    printed "✅ every comparable default matches" at exit 0 — because the
    failures went into ``unscoreable`` and ``main()`` filters exactly that
    prefix out of ``hard_errors``. That is the shape this whole PR is about — a
    gate that is green because nobody is looking — rebuilt inside the gate.

    ⚠️ Not hypothetical: this check's CI leg installs a subset of the 51 tools'
    dependencies, and two of them (``croniter``, ``promql-parser``) are not
    stdlib.
    """

    def test_a_command_that_fails_to_load_is_blind_not_unscoreable(
            self, tmp_path, monkeypatch):
        boom = tmp_path / "boom.py"
        boom.write_text("import a_module_that_does_not_exist\n", encoding="utf-8")
        monkeypatch.setattr(mod, "parse_command_map", lambda: {"boom": "boom.py"})
        monkeypatch.setattr(mod, "_resolve", lambda script: boom)

        captured, unscoreable, blind, faults = mod.introspect_defaults()
        assert captured == {}
        assert not unscoreable, (
            f"a failure to import must NOT land in the soft channel: {unscoreable}")
        assert any("boom" in b and "ModuleNotFoundError" in b for b in blind), blind

    def test_the_soft_channel_still_exists_for_real_repo_facts(
            self, tmp_path, monkeypatch):
        """Control. Without it the assertion above is satisfied by routing
        EVERYTHING to blind, which would red-light the three tools that
        legitimately exit before argparse (batch-pr / guard / parser)."""
        quiet = tmp_path / "quiet.py"
        quiet.write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
        monkeypatch.setattr(mod, "parse_command_map", lambda: {"quiet": "quiet.py"})
        monkeypatch.setattr(mod, "_resolve", lambda script: quiet)

        captured, unscoreable, blind, faults = mod.introspect_defaults()
        assert captured == {}
        assert not blind, f"SystemExit is a repo fact, not blindness: {blind}"
        assert any("quiet" in u for u in unscoreable), unscoreable

    def test_an_empty_command_map_is_a_hard_error(self, monkeypatch):
        """`parse_command_map()` returns {} SILENTLY when it cannot find the
        map. Measured before this: every row fell into `no_parser`, and the run
        printed "✅ every comparable default matches" at exit 0."""
        monkeypatch.setattr(mod, "parse_command_map", lambda: {})
        _c, _u, _b, faults = mod.introspect_defaults()
        assert any("zero commands" in f for f in faults), faults

    def test_a_command_that_vanishes_from_the_scan_surface_is_a_hard_error(self):
        """The accounting identity, not a floor.

        ⛔ Measured: one `skip` line in the introspection loop removed 8
        subcommands — 44% of the comparable rows — and a planted drift went
        invisible while every test stayed green, because the only floor
        (`scored >= 100`) sits in pytest with a 47% cushion under a 188-row
        baseline. A count cannot see that; "every command leaves through
        exactly one of the three doors" can.

        ⚠️ The violation cannot be provoked from outside the module — the loop
        always routes each command somewhere — so the invariant is extracted
        and called directly here. That is the honest shape for a tripwire whose
        trigger is a future source edit.
        """
        three = {"a": "a.py", "b": "b.py", "c": "c.py"}
        assert mod._scan_surface_faults(three, {"a": {}}, ["b"], ["c"]) == [], (
            "control: a fully accounted-for run must produce no fault, or the "
            "negative case below proves nothing")
        fault = mod._scan_surface_faults(three, {"a": {}}, ["b"], [])
        assert fault and "accounted for" in fault[0], fault
        assert "3 commands" in fault[0] and "only 2" in fault[0], fault[0]

    def test_the_identity_holds_on_the_real_repo(self):
        """Positive control for the invariant against the actual tree."""
        cmap = mod.parse_command_map()
        captured, unscoreable, blind, faults = mod.introspect_defaults()
        assert cmap, "COMMAND_MAP is empty; every other assertion here is void"
        assert len(captured) + len(unscoreable) + len(blind) == len(cmap)
        assert not faults, faults

    def test_zero_comparable_rows_is_a_hard_error(self, tmp_path):
        """⛔ Measured: deleting every table row from both cli-reference files
        gave rc=0 and "✅ every comparable default matches". The check has no
        other way to notice that its own input vanished."""
        empty = tmp_path / "empty.md"
        empty.write_text("#### widget\n\nno tables here\n", encoding="utf-8")
        _f, stats, notes, _b = mod.scan([empty], _FAKE_DEFAULTS)
        assert stats["scored"] == 0
        assert any("compared nothing" in n for n in notes), notes

    def test_a_planted_drift_reaches_the_process_exit_code(self, monkeypatch):
        """⛔ The one line this entire PR exists for had NO test.

        Measured: rewriting `if findings and args.ci:` to `if findings and
        False:` left all 24 tests green while the check still PRINTED
        `[DRIFT] …` — stdout said drift, the exit code said fine. Every other
        planted-defect test asserts on `scan()`'s return value and never goes
        through `main()`.
        """
        drift = mod.Finding("docs/x.md", 1, "widget", "--rounds", "`5`", "10")
        clean_stats = {"scored": 1, "matched": 0, "doc_prose": 0,
                       "actual_not_literal": 0, "flag_undeclared": 0,
                       "no_parser": 0}
        monkeypatch.setattr(mod, "scan",
                            lambda *a, **k: ([drift], clean_stats, [], {}))
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--ci"])
        assert mod.main() != 0, (
            "a drift was found and printed, and the process still exited 0")

        # Control: the same path with no findings must stay 0, otherwise the
        # assertion above is satisfied by a check that always fails.
        monkeypatch.setattr(mod, "scan",
                            lambda *a, **k: ([], clean_stats, [], {}))
        assert mod.main() == 0

    def test_blind_reaches_the_exit_code(self, monkeypatch):
        """The split is only worth anything if it changes the verdict."""
        monkeypatch.setattr(
            mod, "scan",
            lambda *a, **k: ([], {"scored": 0, "matched": 0, "doc_prose": 0,
                                  "actual_not_literal": 0, "flag_undeclared": 0,
                                  "no_parser": 0},
                             ["could not load boom — this check saw NOTHING "
                              "for that command"], {}))
        monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--ci"])
        assert mod.main() != 0, (
            "a run that loaded nothing exited 0; that is indistinguishable "
            "from a run that checked everything and found no drift.")
