"""Contract for the runtime encoding gate (tests/_encoding_gate.py, #2005).

Each scenario runs a real pytest in a child process over a throwaway tree laid
out like this repo (``scripts/`` product code, ``tests/`` test code, the gate
copied in unchanged), because the thing under test is interpreter start-up
state: ``sys.flags.warn_default_encoding`` cannot be switched on from inside a
running process.

Both directions, plus the one where nothing can be measured:

* product-code violations fail, in every phase they can run in (collection,
  setup, call, teardown), under xdist too, and a line already reported by
  one test still fails the next test that reaches it;
* the legal and exempt cases pass: ``encoding=`` given, ``subprocess`` with
  ``text=True`` (owner ruling), and test code that omits the encoding;
* flag off: the run passes but says the gate was inactive, and with
  ``VIBE_REQUIRE_ENCODING_GATE=1`` it is a usage error instead — including the
  ``-X`` form under xdist, where the controller has the flag and the workers
  do not.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_DIR.parent
GATE = TESTS_DIR / "_encoding_gate.py"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PROD = '''\
import pathlib, subprocess, sys
def bad_read(p): return pathlib.Path(p).read_text()
def ok_read(p): return pathlib.Path(p).read_text(encoding="utf-8")
def bad_open(p):
    with open(p) as f:
        return f.read()
def sub_text():
    return subprocess.run([sys.executable, "-c", "print(1)"],
                          text=True, capture_output=True).stdout
'''

CONFTEST = '''\
import pathlib, sys
import pytest
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import _encoding_gate

def pytest_configure(config):
    config.pluginmanager.register(_encoding_gate, "encoding-gate")

@pytest.fixture
def data(tmp_path):
    p = tmp_path / "a"
    p.write_text("x", encoding="utf-8")
    return p
'''

CASES = '''\
import pytest
import prod

@pytest.fixture
def bad_setup(data):
    prod.bad_read(data)

@pytest.fixture
def bad_teardown(data):
    yield
    prod.bad_read(data)

def test_call_read_text(data): prod.bad_read(data)
def test_call_open(data): prod.bad_open(data)
def test_same_line_again(data): prod.bad_read(data)
def test_in_setup(bad_setup): pass
def test_in_teardown(bad_teardown): pass
def test_encoding_given(data): prod.ok_read(data)
def test_subprocess_text_is_exempt(): assert prod.sub_text().strip() == "1"
def test_test_code_is_out_of_scope(tmp_path): (tmp_path / "b").write_text("y")
'''

IMPORT_TIME = '''\
import pathlib, tempfile
import prod
_p = pathlib.Path(tempfile.mkdtemp()) / "a"
_p.write_text("x", encoding="utf-8")
prod.bad_read(_p)
def test_never_reached(): pass
'''

VIOLATIONS = {
    ("FAILED", "tests/test_cases.py::test_call_read_text"),
    ("FAILED", "tests/test_cases.py::test_call_open"),
    ("FAILED", "tests/test_cases.py::test_same_line_again"),
    ("ERROR", "tests/test_cases.py::test_in_setup"),
    ("ERROR", "tests/test_cases.py::test_in_teardown"),
    ("ERROR", "tests/test_import_time.py"),
}
LEGAL = {
    ("PASSED", "tests/test_cases.py::test_encoding_given"),
    ("PASSED", "tests/test_cases.py::test_subprocess_text_is_exempt"),
    ("PASSED", "tests/test_cases.py::test_test_code_is_out_of_scope"),
    # the teardown case's call phase is clean; its ERROR is the teardown
    ("PASSED", "tests/test_cases.py::test_in_teardown"),
}

_OUTCOME_RE = re.compile(r"^(PASSED|FAILED|ERROR) (\S+)", re.M)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "prod.py").write_text(PROD, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copy(GATE, tests / "_encoding_gate.py")
    (tests / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tests / "test_cases.py").write_text(CASES, encoding="utf-8")
    (tests / "test_import_time.py").write_text(IMPORT_TIME, encoding="utf-8")
    return tmp_path


def _run(tree: Path, *args: str, flag: bool = False, require: bool = False,
         x_flag: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONWARNDEFAULTENCODING", "VIBE_REQUIRE_ENCODING_GATE",
                        "PYTEST_ADDOPTS", "PYTEST_XDIST_WORKER")}
    if flag:
        env["PYTHONWARNDEFAULTENCODING"] = "1"
    if require:
        env["VIBE_REQUIRE_ENCODING_GATE"] = "1"
    cmd = [sys.executable, *(["-X", "warn_default_encoding"] if x_flag else []),
           "-m", "pytest", "-rA", "-p", "no:cacheprovider",
           "--continue-on-collection-errors", *args, "tests"]
    return subprocess.run(cmd, cwd=tree, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=120)


def _outcomes(out: str) -> set[tuple[str, str]]:
    return set(_OUTCOME_RE.findall(out))


def test_active_gate_fails_every_phase_and_passes_the_legal_cases(tree):
    r = _run(tree, flag=True)
    got = _outcomes(r.stdout)
    assert r.returncode == 1, r.stdout + r.stderr
    assert VIOLATIONS <= got, r.stdout
    assert LEGAL <= got, r.stdout
    assert not got - VIOLATIONS - LEGAL, r.stdout
    for phase in ("collection", "setup", "call", "teardown"):
        assert f"EncodingWarning in product code during {phase}: scripts/prod.py:" in r.stdout
    assert "INACTIVE" not in r.stdout


def test_active_gate_holds_under_xdist_with_the_requirement_set(tree):
    pytest.importorskip("xdist")
    r = _run(tree, "-n", "2", flag=True, require=True)
    got = _outcomes(r.stdout)
    assert r.returncode == 1, r.stdout + r.stderr
    # xdist reports the teardown error and the call pass for the same nodeid
    assert VIOLATIONS <= got, r.stdout
    assert LEGAL <= got, r.stdout


ALONGSIDE = '''\
import pytest
import prod

def test_then_fails(data):
    prod.bad_read(data)
    assert False, "unrelated failure"

def test_then_skips(data):
    prod.bad_read(data)
    pytest.skip("unrelated skip")

def test_skip_alone():
    pytest.skip("unrelated skip")

@pytest.mark.xfail(reason="unrelated xfail")
def test_then_fails_under_xfail_marker(data):
    prod.bad_read(data)
    assert False

@pytest.mark.xfail(strict=True, reason="unrelated strict xfail")
def test_then_passes_under_strict_xfail(data):
    prod.bad_read(data)

def test_then_calls_xfail(data):
    prod.bad_read(data)
    pytest.xfail("unrelated imperative xfail")

@pytest.mark.xfail(reason="unrelated xfail alone")
def test_xfail_alone():
    assert False
'''

ALONGSIDE_AT_IMPORT = IMPORT_TIME.replace("def test_never_reached", "raise RuntimeError('unrelated import error')\ndef test_never_reached")


def test_hits_survive_every_other_outcome_of_the_phase(tree):
    """Nothing else deciding the phase's outcome may swallow the gate's hits:
    another exception, a later skip, an xfail marker (strict or not), or
    pytest.xfail()."""
    tests = tree / "tests"
    (tests / "test_import_time.py").unlink()
    (tests / "test_cases.py").unlink()
    (tests / "test_alongside.py").write_text(ALONGSIDE, encoding="utf-8")
    (tests / "test_alongside_import.py").write_text(ALONGSIDE_AT_IMPORT, encoding="utf-8")
    r = _run(tree, flag=True)
    got = _outcomes(r.stdout)
    assert r.returncode == 1, r.stdout + r.stderr
    assert got == {
        ("FAILED", "tests/test_alongside.py::test_then_fails"),
        ("FAILED", "tests/test_alongside.py::test_then_skips"),
        ("FAILED", "tests/test_alongside.py::test_then_fails_under_xfail_marker"),
        ("FAILED", "tests/test_alongside.py::test_then_passes_under_strict_xfail"),
        ("FAILED", "tests/test_alongside.py::test_then_calls_xfail"),
        ("ERROR", "tests/test_alongside_import.py"),
    }, r.stdout
    # the legal direction: skip and xfail without a hit are left alone
    assert re.findall(r"^XFAIL (\S+)", r.stdout, re.M) == [
        "tests/test_alongside.py::test_xfail_alone"], r.stdout
    # -rA prints skips as "SKIPPED [n] file:line: reason", one line per reason;
    # only test_skip_alone may still be skipped
    assert re.findall(r"^SKIPPED \[(\d+)\] .*unrelated skip$", r.stdout, re.M) == ["1"], r.stdout
    assert "unrelated failure" in r.stdout and "unrelated import error" in r.stdout
    # the gate's own verdicts also print "during call", so the hit kept on a
    # report that failed for another reason is identified by its section header
    # two reports failed for another reason: test_then_fails and the strict XPASS
    kept = re.findall(r"-+ encoding gate -+\nEncodingWarning in product"
                      r" code during call: scripts/prod\.py:", r.stdout)
    assert len(kept) == 2, r.stdout
    assert "EncodingWarning in product code during collection: scripts/prod.py:" in r.stdout


RERUN_CONFTEST = '''
import _pytest.runner

@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Run each item twice on the same Item object, as pytest-rerunfailures does."""
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    for _ in range(2):
        _pytest.runner.runtestprotocol(item, nextitem=nextitem, log=True)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    return True
'''

RERUN_CASES = '''\
import prod

RUNS = []

def test_hit_then_clean(data):
    RUNS.append(1)
    (prod.bad_read if len(RUNS) == 1 else prod.ok_read)(data)

def test_clean_then_hit(data):
    RUNS.append(2)
    (prod.ok_read if RUNS.count(2) == 1 else prod.bad_read)(data)
'''


def test_a_rerun_of_the_same_item_starts_without_the_previous_hits(tree):
    tests = tree / "tests"
    (tests / "test_import_time.py").unlink()
    (tests / "test_cases.py").unlink()
    with (tests / "conftest.py").open("a", encoding="utf-8", newline="\n") as f:
        f.write(RERUN_CONFTEST)
    (tests / "test_rerun.py").write_text(RERUN_CASES, encoding="utf-8")
    r = _run(tree, flag=True)
    lines = re.findall(r"^(PASSED|FAILED) (\S+)", r.stdout, re.M)
    assert sorted(lines) == sorted([
        ("FAILED", "tests/test_rerun.py::test_hit_then_clean"),
        ("PASSED", "tests/test_rerun.py::test_hit_then_clean"),
        ("PASSED", "tests/test_rerun.py::test_clean_then_hit"),
        ("FAILED", "tests/test_rerun.py::test_clean_then_hit"),
    ]), r.stdout


def test_flag_off_passes_and_says_the_gate_was_inactive(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not {o for o in _outcomes(r.stdout) if o[0] != "PASSED"}, r.stdout
    assert "encoding gate: INACTIVE" in r.stdout


def test_flag_off_with_the_requirement_is_a_usage_error(tree):
    r = _run(tree, require=True)
    assert r.returncode == 4, r.stdout + r.stderr
    assert "PYTHONWARNDEFAULTENCODING=1" in r.stderr


def test_x_flag_under_xdist_with_the_requirement_is_refused(tree):
    """-X reaches the controller but not the workers: the workers must refuse."""
    pytest.importorskip("xdist")
    r = _run(tree, "-n", "2", require=True, x_flag=True)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "PASSED" not in r.stdout
    assert "not -X warn_default_encoding" in r.stdout + r.stderr


def test_the_repo_conftest_registers_the_gate(request):
    assert request.config.pluginmanager.get_plugin("_encoding_gate") is not None


def test_the_ci_pytest_job_turns_the_gate_on_and_requires_it():
    job = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]["python-tests-run"]
    env = job.get("env", {})
    assert env.get("PYTHONWARNDEFAULTENCODING") == "1"
    assert env.get("VIBE_REQUIRE_ENCODING_GATE") == "1"
