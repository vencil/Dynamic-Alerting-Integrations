"""Behavioural tests for check_workload_closure_drift.py.

⛔ Written because the lint shipped in PR-A with NONE. The only thing pointing
at it was `test_bilingual_help_contract.py`'s allowlist entry and
`test_tool_exit_codes.py`'s generic bad-flag sweep — neither of which executes
a single comparison. Two real defects survived that gap and were found in
review, not by the suite: a scalar `helpers:` was fed through `list()` and
compared per-character (reporting EXIT_VIOLATION for what is a caller error),
and an unreadable subject file raised a traceback instead of exiting 2.

A lint whose own failure modes are untested is the thing it exists to prevent:
it renders a verdict that looks the same whether or not it checked anything.
Every test here therefore pins an EXIT CODE, because the three codes are the
whole contract — 0 "checked, they agree", 1 "checked, they disagree",
2 "could not check". Collapsing any two of those is the bug class.

The module reads two fixed repo paths as module-level constants, so the tests
retarget `guard.PIN` / `guard.WORKFLOW` with pytest's monkeypatch (auto-restored
per test, so no cross-test leakage — see CLAUDE.md on process-global cleanup).

Subjects under test, named here in full so `verify_diff`'s text_map routes a
change in either of them back to this file (the lint's verdict is only as good
as its agreement with these two, so editing one without re-running this is the
gap that let the closure diverge in the first place):

    .github/bench-reference.yaml                    — the SSOT
    .github/workflows/bench-workload-effect.yaml    — the unavoidable copy
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_workload_closure_drift as guard  # noqa: E402

EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR = 0, 1, 2

_HELPERS = ["config_test.go", "config_debounce_test.go",
            "config_metrics_test.go", "watchloop_test.go"]


def _pin(tmp_path: Path, helpers_yaml: str) -> Path:
    p = tmp_path / "bench-reference.yaml"
    p.write_text(f"workload_closure:\n  derived_glob: '*bench_test.go'\n"
                 f"{helpers_yaml}", encoding="utf-8")
    return p


def _pin_list(tmp_path: Path, helpers=None) -> Path:
    items = _HELPERS if helpers is None else helpers
    body = "  helpers:\n" + "".join(f"    - {h}\n" for h in items)
    return _pin(tmp_path, body)


def _workflow(tmp_path: Path, *lists: list[str]) -> Path:
    """A stand-in workflow carrying `n` quoted literal helper lists."""
    p = tmp_path / "bench-workload-effect.yaml"
    body = ["on:", "  workflow_dispatch:"]
    for i, names in enumerate(lists):
        body.append(f"    input{i}:")
        body.append(f"      default: '{' '.join(names)}'")
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def _run(monkeypatch, pin: Path, workflow: Path) -> int:
    monkeypatch.setattr(guard, "PIN", pin)
    monkeypatch.setattr(guard, "WORKFLOW", workflow)
    monkeypatch.setattr(sys, "argv", ["check_workload_closure_drift.py"])
    return guard.main()


# --- the live repo -----------------------------------------------------------

def test_shipped_repo_is_clean():
    """The real pin file and the real workflow must agree right now.

    This is the regression target: if it fails, a helper was added to one copy
    and not the other, and `W` has quietly become a hybrid tree."""
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS_DIR,
                                      "check_workload_closure_drift.py")],
        capture_output=True, timeout=30)
    assert r.returncode == EXIT_OK, r.stderr.decode("utf-8", "replace")


# --- 0 / 1: it actually compares ---------------------------------------------

def test_agreeing_copies_pass(tmp_path, monkeypatch, capsys):
    rc = _run(monkeypatch, _pin_list(tmp_path), _workflow(tmp_path, _HELPERS))
    assert rc == EXIT_OK
    assert "agree" in capsys.readouterr().out


def test_helper_missing_from_the_workflow_copy_fails(tmp_path, monkeypatch):
    """The exact silent failure this lint exists for: a helper dropped from one
    copy stays pinned to the reference side and W/R stops meaning what it says."""
    short = [h for h in _HELPERS if h != "config_metrics_test.go"]
    assert _run(monkeypatch, _pin_list(tmp_path),
                _workflow(tmp_path, short)) == EXIT_VIOLATION


def test_extra_helper_in_the_workflow_copy_fails(tmp_path, monkeypatch):
    assert _run(monkeypatch, _pin_list(tmp_path),
                _workflow(tmp_path, _HELPERS + ["extra_test.go"])) == EXIT_VIOLATION


def test_reordered_copy_fails(tmp_path, monkeypatch):
    """Order is part of the contract here — the two copies are compared as
    lists, not sets, so a reorder is a disagreement worth surfacing rather than
    a difference the reader has to notice on their own."""
    assert _run(monkeypatch, _pin_list(tmp_path),
                _workflow(tmp_path, list(reversed(_HELPERS)))) == EXIT_VIOLATION


def test_every_literal_copy_is_checked_not_just_the_first(tmp_path, monkeypatch):
    """Two copies exist today (the dispatch default and the PR self-test env).
    A lint that stopped at the first would pass while the second drifted."""
    short = [h for h in _HELPERS if h != "watchloop_test.go"]
    assert _run(monkeypatch, _pin_list(tmp_path),
                _workflow(tmp_path, _HELPERS, short)) == EXIT_VIOLATION


def test_no_literal_list_at_all_fails_rather_than_passing_vacuously(
        tmp_path, monkeypatch):
    """⛔ A lint that matches nothing passes forever. If the duplication is
    genuinely gone the lint should be deleted deliberately, not left running
    green over an empty match set."""
    empty = tmp_path / "bench-workload-effect.yaml"
    empty.write_text("on:\n  workflow_dispatch:\n", encoding="utf-8")
    assert _run(monkeypatch, _pin_list(tmp_path), empty) == EXIT_VIOLATION


# --- 2: "could not check" is never 0 and never 1 -----------------------------

def test_scalar_helpers_is_a_caller_error_not_a_violation(tmp_path, monkeypatch):
    """⛔ Found in review, not by this suite (it did not exist). `list()` on a
    str yields ['c','o','n',...], so the run reported EXIT_VIOLATION with a
    per-character diff — a broken reference point dressed up as a real
    finding, which sends the reader to fix the wrong file."""
    assert _run(monkeypatch, _pin(tmp_path, "  helpers: config_test.go\n"),
                _workflow(tmp_path, _HELPERS)) == EXIT_CALLER_ERROR


@pytest.mark.parametrize("helpers_yaml,why", [
    ("  helpers: []\n", "empty list would make the comparison vacuous"),
    ("  helpers:\n    - ''\n", "empty string member"),
    ("  helpers:\n    - 17\n", "non-string member"),
    ("  helpers: {a: 1}\n", "mapping instead of a list"),
    ("  helpers: null\n", "explicit null"),
    ("  no_helpers_key: []\n", "key absent entirely"),
])
def test_malformed_helpers_are_caller_errors(tmp_path, monkeypatch,
                                             helpers_yaml, why):
    assert _run(monkeypatch, _pin(tmp_path, helpers_yaml),
                _workflow(tmp_path, _HELPERS)) == EXIT_CALLER_ERROR, why


def test_unparseable_pin_yaml_is_a_caller_error(tmp_path, monkeypatch):
    bad = tmp_path / "bench-reference.yaml"
    bad.write_text("workload_closure: [unclosed\n", encoding="utf-8")
    assert _run(monkeypatch, bad, _workflow(tmp_path, _HELPERS)) == EXIT_CALLER_ERROR


def test_missing_pin_file_is_a_caller_error(tmp_path, monkeypatch):
    assert _run(monkeypatch, tmp_path / "nope.yaml",
                _workflow(tmp_path, _HELPERS)) == EXIT_CALLER_ERROR


def test_missing_workflow_file_is_a_caller_error(tmp_path, monkeypatch):
    assert _run(monkeypatch, _pin_list(tmp_path),
                tmp_path / "nope.yaml") == EXIT_CALLER_ERROR


def test_undecodable_workflow_is_a_caller_error_not_a_traceback(
        tmp_path, monkeypatch):
    """⛔ Also found in review: this raised UnicodeDecodeError and died with
    Python's default exit 1, which a CI reader cannot tell from a real
    violation. "Cannot read the subject" is 2."""
    bad = tmp_path / "bench-workload-effect.yaml"
    bad.write_bytes(b"default: '\xff\xfe'\n")
    assert _run(monkeypatch, _pin_list(tmp_path), bad) == EXIT_CALLER_ERROR


def test_undecodable_pin_is_a_caller_error_not_a_traceback(tmp_path, monkeypatch):
    bad = tmp_path / "bench-reference.yaml"
    bad.write_bytes(b"workload_closure: '\xff\xfe'\n")
    assert _run(monkeypatch, bad, _workflow(tmp_path, _HELPERS)) == EXIT_CALLER_ERROR


# --- CLI surface -------------------------------------------------------------

def test_unknown_flag_exits_caller_error():
    """dev-rules #13. Pinned here as well as in the generic sweep because this
    tool takes no options at all — the argparse exists only for this contract,
    so nothing else would notice if it were removed."""
    r = subprocess.run(
        [sys.executable, os.path.join(_TOOLS_DIR,
                                      "check_workload_closure_drift.py"),
         "--no-such-flag"], capture_output=True, timeout=30)
    assert r.returncode == EXIT_CALLER_ERROR
