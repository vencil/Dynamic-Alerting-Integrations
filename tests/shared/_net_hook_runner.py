"""Run an ops tool with an audit hook that logs its network use (#3215).

``python _net_hook_runner.py <tool.py> [args...]`` runs the tool as
``__main__`` (``runpy``, the tool's directory first on ``sys.path`` as for a
direct run) and appends one JSON line per event to ``$NET_HOOK_LOG``, from
any thread of the tool's process:

* ``socket.connect`` — every connection, whatever spelling opened it;
* ``request`` — method and path of the request line ``http.client`` sends
  (urllib goes through it too; HTTPS is seen before TLS);
* ``fetcher`` — ``curl`` / ``wget`` started any way (``subprocess``, a shell,
  ``env``, ``os.system``, ``os.exec*``, ``os.spawn*``);
* ``python-child`` — a Python child that does not run a repo tool
  (``-c``, a script outside ``scripts/tools``): its own network use is
  invisible to this hook, so it is reported instead.

Each line says whether a frame of the lib (``$NET_HOOK_LIB``) was on the
stack, and names the innermost ops frame (``$NET_HOOK_OPS``) if any. Frames
are compared as real paths: tools put ``ops/..`` on ``sys.path``, so the lib
shows up as ``ops/../_lib_prometheus.py``.
"""
from __future__ import annotations

import json
import os
import runpy
import shlex
import sys
import traceback
import urllib.parse

_LOG = os.environ["NET_HOOK_LOG"]
_OPS = os.path.realpath(os.environ["NET_HOOK_OPS"]) + os.sep
_LIB = os.path.realpath(os.environ["NET_HOOK_LIB"])
_TOOLS = os.path.dirname(_LIB) + os.sep
_FETCHERS = {"curl", "wget"}
_METHODS = {b"GET", b"HEAD", b"POST", b"PUT", b"DELETE", b"PATCH", b"OPTIONS"}


def _record(event: str, **fields) -> None:
    frames = [os.path.realpath(f.filename) for f in traceback.extract_stack()[:-2]]
    ops = [f for f in frames if f.startswith(_OPS)]
    line = {"event": event, "frame": os.path.basename(ops[-1]) if ops else "<no ops frame>",
            "via_lib": _LIB in frames, **fields}
    with open(_LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _tokens(parts) -> list[str]:
    out: list[str] = []
    for p in parts:
        p = os.fsdecode(p) if isinstance(p, (bytes, bytearray)) else str(p)
        try:
            out += shlex.split(p)
        except ValueError:
            out += p.split()
    return out


def _command(argv) -> None:
    toks = _tokens(argv)
    if {os.path.basename(t) for t in toks} & _FETCHERS:
        _record("fetcher", argv=toks[:4])
        return
    if toks and os.path.basename(toks[0]).startswith("python"):
        scripts = [t for t in toks[1:] if t.endswith(".py")]
        if not any(os.path.realpath(s).startswith(_TOOLS) for s in scripts):
            _record("python-child", argv=toks[:4])


def _hook(event: str, args: tuple) -> None:
    if event == "socket.connect":
        _record(event, address=str(args[1])[:80])
    elif event == "http.client.send":
        head = bytes(args[1][:300]) if isinstance(args[1], (bytes, bytearray)) else b""
        method, _, rest = head.partition(b" ")
        if method in _METHODS:
            path = rest.split(b" ", 1)[0].decode("latin-1")
            _record("request", method=method.decode(), path=urllib.parse.urlsplit(path).path[:200])
    elif event == "subprocess.Popen":
        _command(args[1] if isinstance(args[1], (list, tuple)) else [args[1]])
    elif event == "os.system":
        _command([args[0]])
    elif event in ("os.exec", "os.spawn"):
        _command(list(args[-2] if event == "os.spawn" else args[1]))


sys.addaudithook(_hook)
_script = os.path.abspath(sys.argv[1])
sys.argv = sys.argv[1:]
sys.path.insert(0, os.path.dirname(_script))
runpy.run_path(_script, run_name="__main__")
