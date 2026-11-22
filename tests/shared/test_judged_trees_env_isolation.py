"""`DA_JUDGED_TREES` does not grow from test to test (tests/conftest.py).

A tool that judges a conf.d tree hands the judgement down to its child
processes through that variable, adding to what its own process wrote
before. A pytest worker is one process for thousands of tests, so the
variable held one temp tree per earlier test and only grew; past the
kernel's limit on one environment string every subprocess the worker
started failed with E2BIG (`OSError: [Errno 7] Argument list too long`) —
2,189 failures in one local run, none in the next, depending on how xdist
dealt the tests out.
"""
import os
import subprocess
import sys
from pathlib import Path

import _lib_parse_cache as pc


def _conftest(pytestconfig):
    want = Path(__file__).resolve().parents[1] / "conftest.py"
    [mod] = [m for m in pytestconfig.pluginmanager.get_plugins()
             if getattr(m, "__file__", None) and Path(m.__file__).resolve() == want]
    return mod


def test_the_env_name_is_the_tools(pytestconfig):
    assert _conftest(pytestconfig)._JUDGED_TREES_ENV == pc.JUDGED_TREES_ENV


def test_every_test_runs_under_the_guard(request):
    assert "_no_judged_tree_outlives_its_test" in request.fixturenames


def test_what_one_test_leaves_is_gone_for_the_next(tmp_path):
    """The guard's teardown itself, through pytest: two tests in one process,
    the first leaving the variable changed (judged_trees_lifecycle_probe.py).
    The helper tests below pass with a fixture that never calls the helper."""
    probe = Path(__file__).with_name("judged_trees_lifecycle_probe.py")
    env = {k: v for k, v in os.environ.items() if k != pc.JUDGED_TREES_ENV}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         "-p", "no:randomly", str(probe)],
        cwd=probe.parents[2], env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "2 passed" in done.stdout, done.stdout[-2000:]


def test_put_back_restores_unset_and_set(pytestconfig, monkeypatch):
    conftest = _conftest(pytestconfig)
    monkeypatch.delenv(pc.JUDGED_TREES_ENV, raising=False)
    os.environ[pc.JUDGED_TREES_ENV] = "grown"
    conftest._judged_trees_put_back(None)
    assert pc.JUDGED_TREES_ENV not in os.environ
    os.environ[pc.JUDGED_TREES_ENV] = "grown"
    conftest._judged_trees_put_back("before")
    assert os.environ[pc.JUDGED_TREES_ENV] == "before"


def test_a_worker_that_judged_many_trees_can_still_start_a_process(pytestconfig, tmp_path):
    """The failure itself: enough hand-downs make the environment too large
    to exec with; put back, the next process starts."""
    conftest = _conftest(pytestconfig)
    before = os.environ.get(pc.JUDGED_TREES_ENV)
    try:
        for i in range(400):  # one string over the 128 KiB a single env entry may be
            pc._hand_down(tmp_path / f"{'t' * 200}-{i:05d}", "0" * 64)
        assert len(os.environ[pc.JUDGED_TREES_ENV]) > 131072
        try:
            subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=60)
        except OSError as exc:
            assert exc.errno == 7, exc
        else:  # a kernel without the per-string limit: nothing to guard there
            return
        conftest._judged_trees_put_back(before)
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=60)
    finally:  # not through the helper under test: a broken one must not leave the worker unable to exec
        if before is None:
            os.environ.pop(pc.JUDGED_TREES_ENV, None)
        else:
            os.environ[pc.JUDGED_TREES_ENV] = before
