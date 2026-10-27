"""Test-only write audit (#3107 v2). Imported by Python at start-up when this
directory is on PYTHONPATH and DA_WRITE_AUDIT_LOG names a log file.

Every write-mode ``open`` / ``os.open``, every ``os.rename``/``os.replace``
and every ``os.chmod`` the process raises is appended to the log as one JSON
line with the stack of repo frames that raised it. The gate test then asks:
did each write to an operator-output path come from ``_lib_io``'s operator
writer, and did any rename land on it?
"""
import json
import os
import sys

_LOG = os.environ.get("DA_WRITE_AUDIT_LOG")
_WRITE_BITS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
if _LOG:
    _fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _busy = False

    def _frames():
        f = sys._getframe(2)
        out = []
        while f is not None:
            fn = f.f_code.co_filename
            if "/scripts/" in fn:
                out.append(f"{os.path.basename(fn)}:{f.f_code.co_name}")
            f = f.f_back
        return out

    def _hook(event, args):
        global _busy
        if _busy:
            return
        rec = None
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int) or path is None:
                return
            if (mode and any(c in mode for c in "wax+")) or (mode is None and flags & _WRITE_BITS):
                rec = {"ev": "open", "path": os.fsdecode(path), "flags": flags}
        elif event == "os.rename":
            rec = {"ev": "rename", "path": os.fsdecode(args[1]), "src": os.fsdecode(args[0])}
        elif event == "os.chmod":
            if isinstance(args[0], int):
                return
            rec = {"ev": "chmod", "path": os.fsdecode(args[0])}
        if rec is None:
            return
        _busy = True
        try:
            rec["stack"] = _frames()
            os.write(_fd, (json.dumps(rec) + "\n").encode())
        finally:
            _busy = False

    sys.addaudithook(_hook)
