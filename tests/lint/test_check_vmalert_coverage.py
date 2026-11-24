"""Tests for check_vmalert_coverage.py — rule-pack alert firing-coverage baseline guard.

Pinned contracts
----------------
1. **clean → pass**: when the baseline == the current uncovered set, exit 0.
2. **new gap → fail** (headline): a declared alert with no alert_rule_test and NOT in the
   baseline exits 1 — it must not silently escape both firing gates.
3. **healed → fail**: a baseline entry that now HAS a firing test exits 1 (baseline must
   stay == reality; stale entries get removed).
4. **removed/renamed → fail**: an alert listed in the baseline but no longer declared exits
   1 (also a stale baseline entry).
5. **threshold-only fixture ≠ coverage**: an alert exercised only by a promql_expr_test (the
   value contract) is still "uncovered" — firing coverage needs alert_rule_test.
6. **--generate → reality**: generating then checking passes (baseline == tree).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_vmalert_coverage as cov  # noqa: E402


def _pack(alerts: list[str]) -> str:
    rules = "\n".join(f"      - alert: {a}\n        expr: up == 0" for a in alerts)
    return f"groups:\n  - name: g\n    rules:\n{rules}\n"


def _fixture_alert_test(alertnames: list[str]) -> str:
    arts = "\n".join(f"      - eval_time: 1m\n        alertname: {a}\n        exp_alerts: []"
                     for a in alertnames)
    return ("rule_files: []\ntests:\n  - interval: 1m\n    input_series: []\n"
            f"    alert_rule_test:\n{arts}\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Minimal rule-packs/ + tests/rulepacks/ tree, module globals repointed at it."""
    rp = tmp_path / "rule-packs"
    tr = tmp_path / "tests" / "rulepacks"
    rp.mkdir(parents=True)
    tr.mkdir(parents=True)
    monkeypatch.setattr(cov, "_REPO", tmp_path)
    monkeypatch.setattr(cov, "_RULE_PACKS", rp)
    monkeypatch.setattr(cov, "_TESTS", tr)
    monkeypatch.setattr(cov, "_BASELINE", tr / "vmalert_coverage_baseline.yaml")
    return rp, tr


def _write_baseline(tr: Path, uncovered: dict) -> None:
    (tr / "vmalert_coverage_baseline.yaml").write_text(
        yaml.safe_dump({"uncovered": uncovered}), encoding="utf-8")


def test_clean_passes(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Buncovered"]})   # B grandfathered
    assert cov.check() == 0


def test_new_gap_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {})   # B is NOT grandfathered -> new silent gap
    assert cov.check() == 1


def test_healed_baseline_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Bnow"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested", "Bnow"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Bnow"]})   # stale: Bnow now tested
    assert cov.check() == 1


def test_removed_alert_stale_baseline_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Gone"]})   # Gone no longer declared
    assert cov.check() == 1


def test_threshold_only_fixture_is_not_firing_coverage(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Cthreshold"]), encoding="utf-8")
    # a promql_expr_test (value contract), NOT an alert_rule_test -> not firing coverage
    (tr / "x-threshold_test.yaml").write_text(
        "rule_files: []\ntests:\n  - interval: 1m\n    input_series: []\n"
        "    promql_expr_test:\n      - expr: tenant:alert_threshold:c\n        eval_time: 1m\n"
        "        exp_samples: []\n", encoding="utf-8")
    _write_baseline(tr, {})            # not grandfathered
    assert cov.check() == 1            # still counted as an uncovered firing decision


def test_generate_makes_reality(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    cov.generate()
    assert cov.load_baseline() == {"rule-pack-x.yaml": ["Buncovered"]}
    assert cov.check() == 0


def test_duplicate_alertname_across_packs_fails_loud(tree):
    """A cross-pack alertname collision must FAIL LOUDLY (CodeRabbit #969) — the global
    "covered" namespace would otherwise silently drop one pack's alert / false-cover it."""
    rp, tr = tree
    (rp / "rule-pack-a.yaml").write_text(_pack(["Dup"]), encoding="utf-8")
    (rp / "rule-pack-b.yaml").write_text(_pack(["Dup"]), encoding="utf-8")   # same name, 2 packs
    _write_baseline(tr, {})
    with pytest.raises(ValueError, match="duplicate alertname"):
        cov.declared_alerts()


def test_main_reports_duplicate_as_violation_not_traceback(tree, monkeypatch):
    rp, tr = tree
    (rp / "rule-pack-a.yaml").write_text(_pack(["Dup"]), encoding="utf-8")
    (rp / "rule-pack-b.yaml").write_text(_pack(["Dup"]), encoding="utf-8")
    _write_baseline(tr, {})
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() == 1   # graceful violation exit, not an uncaught ValueError


@pytest.mark.parametrize("content", [b"tests: [\xff\xfe]\n", b"tests: [unclosed\n",
                                     b"- a\n- b\n"],
                         ids=["not-utf8", "not-yaml", "not-a-mapping"])
def test_an_unreadable_fixture_is_a_caller_error_not_a_finding(tree, monkeypatch, content):
    """A fixture the tool cannot read means nothing was checked: 2, never the 1 of
    a coverage finding (#3577). Before, a decode error rode the duplicate-alertname
    `except ValueError` and was reported as a violation."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["A"]), encoding="utf-8")
    (tr / "x_test.yaml").write_bytes(content)
    _write_baseline(tr, {})
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() == 2


def test_yml_rule_pack_is_not_silently_ignored(tree):
    """A `.yml`-suffixed pack must be tracked, not skipped by a `.yaml`-only glob (Gemini #969)
    — otherwise its alerts fail OPEN (no test, not baselined, straight past the gate)."""
    rp, tr = tree
    (rp / "rule-pack-legacy.yml").write_text(_pack(["YmlAlert"]), encoding="utf-8")   # .yml!
    _write_baseline(tr, {})
    assert "YmlAlert" in cov.declared_alerts()   # tracked
    assert cov.check() == 1                       # untested + un-baselined -> NEW gap, fail-closed


def test_yml_fixture_counts_as_coverage(tree):
    """A `*_test.yml` fixture must count as coverage, symmetric with the rule-pack `.yml` defence."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["A"]), encoding="utf-8")
    (tr / "x_test.yml").write_text(_fixture_alert_test(["A"]), encoding="utf-8")   # .yml fixture
    _write_baseline(tr, {})
    assert "A" in cov.tested_alertnames()
    assert cov.check() == 0                       # A covered by the .yml fixture -> clean


# #3206: a pack kubectl's plain YAML reading refuses cannot be ruled out as a
# rule file — it goes to Prometheus' rule loader, whose refusal fails the
# gate naming it (it used to be dropped as "not a rule file": fail-open).
_TAB_BROKEN_PACK = "groups:\n  - name: x\n    rules:\n\t- alert: BrokenTabAlert\n      expr: up == 0\n"


def test_pack_no_reader_reads_fails_naming_it(tree, monkeypatch, capsys):
    rp, tr = tree
    (rp / "rule-pack-broken.yaml").write_text(_TAB_BROKEN_PACK, encoding="utf-8")
    (rp / "threshold-registry.yaml").write_text("entries: []\n", encoding="utf-8")
    _write_baseline(tr, {})
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() != 0
    assert "rule-pack-broken.yaml" in capsys.readouterr().err


def test_yaml_without_groups_is_not_a_pack(tree):
    rp, tr = tree
    (rp / "threshold-registry.yaml").write_text("entries: []\n", encoding="utf-8")
    _write_baseline(tr, {})
    assert cov.declared_alerts() == {}
    assert cov.check() == 0


def test_an_undeclared_yaml_beside_the_packs_fails_naming_it(tree, monkeypatch, capsys):
    """#3426: the packs are declared by path (rule-pack-*.yaml|yml, and the
    registry beside them); a pack named another way is named, rc 2 — it
    cannot escape the gate (Gemini #969), and it is not judged by content."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["A"]), encoding="utf-8")
    (rp / "extra-pack.yaml").write_text(_pack(["Hidden"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["A"]})
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() == 2
    assert "extra-pack.yaml" in capsys.readouterr().err


def test_an_empty_rule_pack_fails_naming_it(tree, monkeypatch, capsys):
    """#3426 F14: a declared pack with no object and no `groups` is named,
    rc 2 — never a pack with no alerts."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["A"]), encoding="utf-8")
    (rp / "rule-pack-empty.yaml").write_text("# todo\n", encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["A"]})
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() == 2
    assert "rule-pack-empty.yaml" in capsys.readouterr().err


def test_repo_rule_packs_pass(monkeypatch):
    """smoke：repo 真實的 rule-packs/ → rc 0。"""
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--ci"])
    assert cov.main() == 0


@pytest.mark.parametrize("mode", ["--ci", "--generate"])
def test_a_baseline_that_is_a_fifo_is_rc2_one_line(mode, tree, monkeypatch, capsys):
    """#3981: the baseline is read (--ci) or rewritten (--generate); a FIFO
    there blocked both forever. Refused before the read, and the write goes
    through the artifact entry: rc 2, one standard line naming the file."""
    import signal
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    rp, tr = tree
    baseline = tr / "vmalert_coverage_baseline.yaml"
    os.mkfifo(baseline)
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", mode])

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")
    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        with pytest.raises(SystemExit) as exc:
            cov.main()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
    err = capsys.readouterr().err
    assert exc.value.code == 2 and str(baseline) in err, err
    assert len(err.strip().splitlines()) == 1 and err.startswith("ERROR: cannot "), err
    if mode == "--ci":  # a read: the hint must not speak of an unwritable output
        assert "unreadable" in err and "unwritable" not in err, err


def test_an_unchanged_baseline_on_stdin_is_left_alone(tree, monkeypatch, capsys):
    """#3981 (review): --generate replaces the baseline only when it changes,
    so a current baseline that is also this process's stdin stays rc 0 (the
    entry refuses to replace a standard stream)."""
    rp, tr = tree
    baseline = tr / "vmalert_coverage_baseline.yaml"
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--generate"])
    assert cov.main() == 0
    before = baseline.read_bytes()
    saved = os.dup(0)
    try:
        with open(baseline, "rb") as fh:
            os.dup2(fh.fileno(), 0)
        rc = cov.main()
    finally:
        os.dup2(saved, 0)
        os.close(saved)
    assert rc == 0, capsys.readouterr().err
    assert baseline.read_bytes() == before


def test_a_crlf_copy_of_the_baseline_is_rewritten(tree, monkeypatch):
    """#3981 (review): "unchanged" is judged on bytes, so a CRLF copy of the
    current baseline is rewritten to LF rather than kept."""
    rp, tr = tree
    baseline = tr / "vmalert_coverage_baseline.yaml"
    monkeypatch.setattr(sys, "argv", ["check_vmalert_coverage.py", "--generate"])
    assert cov.main() == 0
    golden = baseline.read_bytes()
    baseline.write_bytes(golden.replace(b"\n", b"\r\n"))
    assert cov.main() == 0
    assert baseline.read_bytes() == golden
