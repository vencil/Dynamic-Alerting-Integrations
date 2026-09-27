"""Runtime gate: product code must not open text files without an encoding.

``read_text()`` / ``write_text()`` / ``Path.open()`` without ``encoding=`` are
invisible to static checks — the receiver's type is not in the syntax (the
``open-encoding-audit`` hook sees only builtin ``open``, and ruff ``PLW1514``
the same). The interpreter does know: with ``PYTHONWARNDEFAULTENCODING=1`` it
emits ``EncodingWarning`` at the call site. This plugin turns that warning into
a test failure when the call site is under ``scripts/`` or ``components/``.

What fails, and where
---------------------
Every phase a test's code can run in is watched, each by its own hook, so the
report names the phase: collection (module-level code of a test file), setup,
call, teardown. ⛔ Not one ``pytest_runtest_protocol`` wrapper instead of the
three runtest hooks: an exception raised there is not a test report, it is an
INTERNALERROR, and the test is still counted as passed.

The verdict is applied to the phase's report (see ``pytest_runtest_makereport``
below): a phase with hits fails even if it would have passed, skipped or
xfailed; one that failed for another reason keeps them as a report section
("encoding gate").

What is deliberately not caught
-------------------------------
* ``subprocess`` with ``text=True`` and no encoding (owner ruling, #2005). The
  warning's filename is the product caller, so it is told apart by the stack:
  any frame running in the ``subprocess`` module exempts it.
* Warnings under ``tests/`` — test code, not product code.
* Code running in a child process: a tool run via ``subprocess``, or a
  ``multiprocessing`` fork. The child's warning never reaches this process.
  Deferred (#2005) until an encoding bug turns up in a code path that only a
  subprocess-driven test reaches. (A fork child's warning is not visible
  without this gate either: pytest's own warning capture is forked with it.)

Activation
----------
``sys.flags.warn_default_encoding`` is fixed at interpreter start, so this
plugin cannot switch it on. ⛔ Set it with the environment variable, not
``-X warn_default_encoding``: pytest-xdist workers inherit the environment but
not ``-X`` flags, so under ``-n`` the flag form leaves every worker unwatched
and the run passes.

When the flag is off the gate does nothing and says so in the terminal
summary. ``VIBE_REQUIRE_ENCODING_GATE=1`` (set by the CI job that runs the
suite) turns "off" into a usage error, so a CI job that lost the variable
fails instead of passing unwatched.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCT_ROOTS = tuple((REPO_ROOT / d).resolve() for d in ("scripts", "components"))
REQUIRE_ENV = "VIBE_REQUIRE_ENCODING_GATE"
SECTION = "encoding gate"


def active() -> bool:
    return bool(sys.flags.warn_default_encoding)


def _via_subprocess(frame) -> bool:
    while frame is not None:
        if frame.f_globals is subprocess.__dict__:
            return True
        frame = frame.f_back
    return False


def _product_site(filename: str) -> str | None:
    """``filename`` relative to the repo when it is product code, else None."""
    if filename.startswith("<"):        # <frozen os>, <string>, ...
        return None
    path = Path(filename).resolve()
    if not any(path.is_relative_to(root) for root in PRODUCT_ROOTS):
        return None
    return path.relative_to(REPO_ROOT).as_posix()


@contextlib.contextmanager
def _watch(hits: list[str]):
    with warnings.catch_warnings():
        # "always", not "default": recording then does not depend on the
        # per-module __warningregistry__ bookkeeping. With "default" a line is
        # reported once per phase (the registry is reset whenever the filters
        # change, so each phase starts clean); the test fails either way.
        warnings.simplefilter("always", EncodingWarning)
        prev = warnings.showwarning

        def show(message, category, filename, lineno, file=None, line=None):
            if category is EncodingWarning and not _via_subprocess(sys._getframe(1)):
                site = _product_site(filename)
                if site is not None:
                    hits.append(f"{site}:{lineno}")
                    return
            prev(message, category, filename, lineno, file, line)

        warnings.showwarning = show
        yield


def _message(phase: str, hits: list[str]) -> str:
    return (f"EncodingWarning in product code during {phase}: {', '.join(hits)}"
            " — pass encoding= explicitly (see tests/_encoding_gate.py)")


_HITS = pytest.StashKey[dict]()


def _runtest_hook(phase: str):
    @pytest.hookimpl(wrapper=True)
    def hook(item):
        if not active():
            return (yield)
        hits: list[str] = []
        try:
            with _watch(hits):
                return (yield)
        finally:
            if hits:
                item.stash.setdefault(_HITS, {})[phase] = hits
    return hook


pytest_runtest_setup = _runtest_hook("setup")
pytest_runtest_call = _runtest_hook("call")
pytest_runtest_teardown = _runtest_hook("teardown")


# ⛔ The verdict is taken on the REPORT, not by raising from the phase hooks.
# Raising only works when nothing else decides the outcome: another exception
# in the same phase, a later pytest.skip(), pytest.xfail() and an xfail marker
# each turn the phase into a report that either hides the hits (skipped and
# xfailed reports show no sections) or drops them. This wrapper has to sit
# outside the skipping plugin's, to see the report after xfail is applied: a
# plugin registered later is already outside it, tryfirst keeps it outside
# wrappers registered later still.
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    # pop, not get: a rerun of the same item (pytest-rerunfailures) reuses the
    # item and its stash, and must not inherit the previous run's hits
    hits = item.stash.get(_HITS, {}).pop(call.when, None)
    if hits:
        message = _message(call.when, hits)
        if report.failed:
            report.sections.append((SECTION, message))
        else:
            report.outcome = "failed"
            report.longrepr = message
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector):
    if not active():
        return (yield)
    hits: list[str] = []
    with _watch(hits):
        report = yield
    if hits:
        if report.outcome == "failed":
            report.sections.append((SECTION, _message("collection", hits)))
        else:
            report.outcome = "failed"
            report.longrepr = _message("collection", hits)
    return report


def pytest_configure(config):
    if not active() and os.environ.get(REQUIRE_ENV) == "1":
        raise pytest.UsageError(
            f"{REQUIRE_ENV}=1 but sys.flags.warn_default_encoding is off: set"
            " PYTHONWARNDEFAULTENCODING=1 (not -X warn_default_encoding, which"
            " pytest-xdist workers do not inherit)")


def pytest_terminal_summary(terminalreporter):
    if not active():
        terminalreporter.write_line(
            "encoding gate: INACTIVE (PYTHONWARNDEFAULTENCODING is not set);"
            " EncodingWarning in scripts/ and components/ was not checked")
