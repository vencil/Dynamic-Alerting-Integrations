"""Run an ops tool with an audit hook that logs its network use (#3215).

``python _net_hook_runner.py <tool.py> [args...]`` runs the tool as
``__main__`` (``runpy``, the tool's directory first on ``sys.path`` as for a
direct run) and appends one JSON line per event to ``$NET_HOOK_LOG``:

* ``socket.connect`` — every connection, whatever spelling opened it;
* ``urllib.Request`` — the URL and method, when urllib built the request;
* ``subprocess.Popen`` of ``curl`` / ``wget`` — HTTP from outside Python.

Each line says whether a frame of the lib (``$NET_HOOK_LIB``) was on the
stack. Frames are compared as real paths: tools put ``ops/..`` on
``sys.path``, so the lib shows up as ``ops/../_lib_prometheus.py``.
Only events with a frame under ``$NET_HOOK_OPS`` are logged.
"""
from __future__ import annotations

import json
import os
import runpy
import sys
import traceback

_LOG = os.environ["NET_HOOK_LOG"]
_OPS = os.path.realpath(os.environ["NET_HOOK_OPS"]) + os.sep
_LIB = os.path.realpath(os.environ["NET_HOOK_LIB"])
_FETCHERS = {"curl", "wget"}


def _record(event: str, **fields) -> None:
    frames = [os.path.realpath(f.filename) for f in traceback.extract_stack()[:-2]]
    ops = [f for f in frames if f.startswith(_OPS)]
    if not ops:
        return
    line = {"event": event, "frame": os.path.basename(ops[-1]),
            "via_lib": _LIB in frames, **fields}
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _hook(event: str, args: tuple) -> None:
    if event == "socket.connect":
        _record(event, address=str(args[1])[:80])
    elif event == "urllib.Request":
        _record(event, url=str(args[0])[:200], method=str(args[3] or "GET"))
    elif event == "subprocess.Popen":
        argv = [str(a) for a in (args[1] if isinstance(args[1], (list, tuple)) else [args[1]])]
        names = {os.path.basename(str(args[0]))} | {os.path.basename(a) for a in argv[:1]}
        if names & _FETCHERS:
            _record(event, argv=argv[:3])


sys.addaudithook(_hook)
_script = os.path.abspath(sys.argv[1])
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(_script))
runpy.run_path(_script, run_name="__main__")
