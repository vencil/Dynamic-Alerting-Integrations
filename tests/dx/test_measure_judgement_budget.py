"""Tests for measure_judgement_budget.py (#3206, ADR-036 §3 "快取的去留").

The measurement itself (helm, amtool, a 5000-tenant judgement) never runs
here. What is pinned is the part that turns runs into an answer, because a
wrong answer there is silent:

  1. A run the memory limit ended — MemoryError, or ANY program (a Go one
     ends by SIGABRT under GOTRACEBACK=crash) or the judgement process itself
     ended by a signal — is a measured "memory does not hold" (exit 1). It is
     never read as a pass, and never as "not measured".
  2. A run with no verdict (the generator's exit is not 0/1, e.g. a Go child's
     out-of-memory read as exit 2) or no usable served-values document is not
     a measurement (exit 2), never a pass.
  3. A run with no report (the judgement process died) does not crash the
     summary.
  4. Missing tools / tree are exit 2 before anything is created.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "measure_judgement_budget.py"
_spec = importlib.util.spec_from_file_location("measure_judgement_budget", _PATH)
mjb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mjb)

MiB = 1024 * 1024
LIMIT = 192 * MiB
LIMITS = {
    "memory_limit_bytes": LIMIT, "memory_limit_from": "test", "deadline_seconds": 20.0,
    "deadline_from": "test", "policy": None, "cpu_limit": "1", "cpus": 1, "cpu_from": "test",
}
WRITE = {"kind": "tenant file", "path": "a/t.yaml", "content_b64": "", "tenants": ["t1"],
         "tenant_count_in_tree": 1}


def _child(label: str, exit_: int = 0, kib: int = 50 * 1024) -> dict:
    return {"argv": [label], "label": label, "exit": exit_, "maxrss_kib": kib,
            "wall_s": 0.1, "launched": True}


def _run(limit=LIMIT, *, gen_rc=0, children=None, sv_ok=True, memory_error=None,
         report=True, exit_=0, wall=5.0, self_kib=60 * 1024) -> dict:
    if children is None:
        children = [_child("da-guard parse-tree (tree)"), _child("amtool check-config"),
                    _child("da-guard served-values")]
    rep = None
    if report:
        rep = {"affinity": [0], "rlimit_data": limit, "gotraceback": "crash", "judge_s": wall,
               "stages": {}, "generator_rc": gen_rc, "memory_error": memory_error,
               "findings": {}, "served_values_rc": 0 if sv_ok else 2,
               "served_values_ok": sv_ok, "tenants_absent": [],
               "self_hwm_kib": self_kib, "self_maxrss_kib": self_kib + 30 * 1024,
               "children": children}
    run = {"limit": limit, "exit": exit_, "hung": False, "wall_s": wall, "report": rep,
           "stderr_tail": ""}
    run["outcome"], run["why"] = mjb.outcome(run)
    return run


# ── classification ───────────────────────────────────────────────────────

def test_a_whole_judgement_is_complete():
    assert _run()["outcome"] == "complete"
    assert _run(gen_rc=1)["outcome"] == "complete"  # findings that block are a verdict


@pytest.mark.parametrize("label", ["da-guard parse-tree (tree)", "amtool check-config",
                                   "da-guard served-values", "git cat-file"])
def test_any_program_ended_by_a_signal_under_the_limit_is_memory(label):
    children = [_child("da-guard parse-tree (tree)"), _child("amtool check-config"),
                _child("da-guard served-values"), _child("git cat-file")]
    for c in children:
        if c["label"] == label:
            c["exit"] = -signal.SIGABRT
    r = _run(children=children)
    assert r["outcome"] == "memory", r
    assert label in r["why"]


def test_the_same_signal_with_no_limit_is_not_a_memory_result():
    children = [_child("da-guard parse-tree (tree)", -signal.SIGABRT)]
    assert _run(limit=None, children=children)["outcome"] == "failed"


def test_a_go_child_out_of_memory_read_as_exit_2_is_no_verdict_not_a_pass():
    # amtool (or any Go program) without GOTRACEBACK=crash exits 2 on OOM; the
    # generator then exits 2. That is no verdict: never complete.
    children = [_child("da-guard parse-tree (tree)"), _child("amtool check-config", 2),
                _child("da-guard served-values")]
    r = _run(gen_rc=2, children=children)
    assert r["outcome"] == "incomplete"
    assert "no verdict" in r["why"]


def test_served_values_without_a_document_is_not_complete():
    assert _run(sv_ok=False)["outcome"] == "incomplete"


def test_a_memory_error_is_memory():
    assert _run(memory_error="da-guard parse-tree was ended by SIGABRT")["outcome"] == "memory"


def test_a_dead_judgement_process_is_memory_under_the_limit():
    r = _run(report=False, exit_=-signal.SIGKILL)
    assert r["outcome"] == "memory"
    assert _run(report=False, exit_=1)["outcome"] == "incomplete"


def test_no_tree_mode_parse_tree_is_not_the_cache_less_path():
    children = [_child("da-guard parse-tree (file)"), _child("amtool check-config"),
                _child("da-guard served-values")]
    assert _run(children=children)["outcome"] == "incomplete"


# ── measure(): exit 0 / 1 / 2 ────────────────────────────────────────────

def _args(tmp_path: Path, **kw) -> argparse.Namespace:
    base = dict(tree=None, count=1, seed=1, runs=3, chart="c", values=None,
                da_guard_binary=None, work_dir=str(tmp_path / "work"), _judge=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def staged(monkeypatch, tmp_path):
    """measure() with every outside step replaced; `plan` maps the job name's
    kind (run / fraction / unlimited) to a run factory."""
    monkeypatch.setattr(mjb, "_preflight", lambda a: "/bin/true")
    monkeypatch.setattr(mjb, "read_limits", lambda chart, values: dict(LIMITS))
    monkeypatch.setattr(mjb, "prepare_repo", lambda *a: str(tmp_path / "repo"))
    monkeypatch.setattr(mjb, "choose_writes", lambda repo, g: [dict(WRITE)])
    monkeypatch.setattr(mjb, "_git", lambda repo, *a: "0" * 40)
    monkeypatch.setattr(os, "sched_getaffinity", lambda pid: {0})
    plan = {"run": lambda lim: _run(lim), "fraction": lambda lim: _run(lim),
            "unlimited": lambda lim: _run(lim)}

    def fake_run(spec, cpus, limit):
        name = os.path.basename(spec["job_dir"]).split("-", 1)[1]
        return plan["run" if name.startswith("run") else name](limit)

    monkeypatch.setattr(mjb, "run_judgement", fake_run)
    return plan


def test_exit_0_when_both_conditions_hold(staged, tmp_path, capsys):
    assert mjb.measure(_args(tmp_path)) == mjb.EXIT_HOLD
    assert "OVERALL (worst write): memory holds; time holds" in capsys.readouterr().out


def test_exit_1_when_a_run_is_cut_by_the_limit(staged, tmp_path):
    staged["run"] = lambda lim: _run(lim, children=[
        _child("da-guard parse-tree (tree)", -signal.SIGABRT), _child("da-guard served-values")])
    assert mjb.measure(_args(tmp_path)) == mjb.EXIT_NOT_HOLD


def test_exit_1_when_the_fraction_run_is_cut(staged, tmp_path):
    staged["fraction"] = lambda lim: _run(lim, memory_error="MemoryError")
    assert mjb.measure(_args(tmp_path)) == mjb.EXIT_NOT_HOLD


def test_exit_1_on_peak_or_time_over_the_rule(staged, tmp_path):
    staged["run"] = lambda lim: _run(lim, self_kib=150 * 1024)
    assert mjb.measure(_args(tmp_path)) == mjb.EXIT_NOT_HOLD
    staged["run"] = lambda lim: _run(lim, wall=10.5)
    assert mjb.measure(_args(tmp_path, work_dir=str(tmp_path / "w2"))) == mjb.EXIT_NOT_HOLD


def test_every_run_without_a_report_is_exit_1_not_a_crash(staged, tmp_path, capsys):
    staged["run"] = lambda lim: _run(lim, report=False, exit_=-signal.SIGKILL)
    staged["fraction"] = staged["run"]
    assert mjb.measure(_args(tmp_path)) == mjb.EXIT_NOT_HOLD
    assert "DOES NOT HOLD" in capsys.readouterr().out


def test_exit_2_on_a_go_child_oom_read_as_no_verdict(staged, tmp_path, capsys):
    staged["run"] = lambda lim: _run(lim, gen_rc=2)
    assert mjb.main(["--work-dir", str(tmp_path / "work")]) == mjb.EXIT_NOT_MEASURED
    assert "no verdict" in capsys.readouterr().err


def test_missing_amtool_is_exit_2_and_creates_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mjb.shutil, "which", lambda name: None)
    work = tmp_path / "work"
    rc = mjb.main(["--da-guard-binary", sys.executable, "--work-dir", str(work)])
    assert rc == mjb.EXIT_NOT_MEASURED
    assert "amtool" in capsys.readouterr().err
    assert not work.exists()


def test_missing_tree_is_exit_2_and_creates_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mjb.shutil, "which", lambda name: "/usr/bin/" + name)
    work = tmp_path / "work"
    rc = mjb.main(["--da-guard-binary", sys.executable, "--tree", str(tmp_path / "nope"),
                   "--work-dir", str(work)])
    assert rc == mjb.EXIT_NOT_MEASURED
    assert "--tree" in capsys.readouterr().err
    assert not work.exists()


def test_importing_the_module_patches_nothing():
    import subprocess
    assert os.waitpid is not mjb._waitpid_with_rusage
    assert subprocess.Popen.__init__ is not mjb._popen_init
