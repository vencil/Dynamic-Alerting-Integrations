"""Test-only listing audit (#3446). Imported by Python at start-up when this
directory is on PYTHONPATH and DA_INPUT_AUDIT_LOG names a log file.

Only events whose argument IS the sentinel directory named by
DA_INPUT_AUDIT_SENTINEL are logged — ``os.listdir`` / ``os.scandir`` /
``os.walk`` on it, ``glob.glob`` of a pattern directly under it, ``open`` of
it — one JSON line each, with the base names of the files of every frame
that raised it. The gate test then asks: did each come from ``_lib_input``?
No vocabulary is read: what is checked is which code touched the path.
"""
import json
import os
import sys

_LOG = os.environ.get("DA_INPUT_AUDIT_LOG")
_SENTINEL = os.environ.get("DA_INPUT_AUDIT_SENTINEL")
if _LOG and _SENTINEL:
    _fd = os.open(_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    _real = os.path.realpath(_SENTINEL)
    _busy = False

    def _is_sentinel(path):
        if path is None or isinstance(path, int):
            return False
        try:
            return os.path.realpath(os.fsdecode(path)) == _real
        except (TypeError, ValueError):
            return False

    def _frames():
        f = sys._getframe(2)
        out = []
        while f is not None:
            out.append(f"{os.path.basename(f.f_code.co_filename)}:{f.f_code.co_name}")
            f = f.f_back
        return out

    def _hook(event, args):
        global _busy
        if _busy:
            return
        if event in ("os.listdir", "os.scandir", "os.walk", "open"):
            path = args[0] if args else None
            # os.listdir() / os.scandir() with no argument list ".".
            if event in ("os.listdir", "os.scandir") and path is None:
                path = "."
            if not _is_sentinel(path):
                return
        elif event == "glob.glob":
            pattern = args[0]
            if isinstance(pattern, bytes):
                pattern = os.fsdecode(pattern)
            if not (_is_sentinel(pattern) or _is_sentinel(os.path.dirname(pattern))):
                return
            path = pattern
        else:
            return
        _busy = True
        try:
            rec = {"ev": event, "path": os.fsdecode(path), "stack": _frames()}
            os.write(_fd, (json.dumps(rec) + "\n").encode())
        finally:
            _busy = False

    sys.addaudithook(_hook)
