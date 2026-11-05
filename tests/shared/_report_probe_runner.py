"""Run a tool and log every Report it hands back to ``run()`` (#3558).

``python _report_probe_runner.py <tool.py> [args...]`` runs the tool as
``__main__`` (``runpy``, the tool's directory first on ``sys.path`` as for a
direct run), with ``_lib_exitcodes.emit_result`` — the one step through which
``run()`` writes a Report — wrapped to append one line per Report to
``$REPORT_PROBE_LOG``. ``$REPORT_PROBE_TOOLS`` is the ``scripts/tools`` the
tool will import the library from (a sandbox copy has its own).

What the stdout gate (test_json_stdout_contract.py) reads from it: whether
the tool returned a Report at all. A tool that printed its document itself
leaves the log empty while stdout is not — the invariant it asserts.
"""
from __future__ import annotations

import os
import runpy
import sys

_LOG = os.environ["REPORT_PROBE_LOG"]
sys.path.insert(0, os.environ["REPORT_PROBE_TOOLS"])
import _lib_exitcodes  # noqa: E402

_real = _lib_exitcodes.emit_result


def _emit_result(result):
    if isinstance(result, _lib_exitcodes.Report):
        with open(_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"status={result.status} delivery={result.delivery}\n")
    return _real(result)


_lib_exitcodes.emit_result = _emit_result

script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(os.path.abspath(script)))
runpy.run_path(script, run_name="__main__")
