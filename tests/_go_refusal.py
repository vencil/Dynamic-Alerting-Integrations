"""Go's answer to "does a conf.d tool refuse this tree", for tests (#3206).

Every conf.d tool refuses a tree on which `da-guard parse-tree` gives any
entry a `verdict` (the shared fail-closed exit,
`_lib_parse_tree.refuse_unservable`): exit 2 (a tool's own caller-error code
where its documented contract says so), one `ERROR: <path>:` line per such
path. A test asks Go which paths those are (`go_refused`) and holds the run
to the refusal (`assert_refused`) — it never composes the list itself.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path


def go_refused(conf_d: "str | os.PathLike[str]", binary: "str | None" = None) -> "list[str]":
    """The root-relative paths `da-guard parse-tree` gives a verdict on
    *conf_d* (sorted; [] for a tree every conf.d tool reads)."""
    exe = binary or os.environ.get("DA_GUARD_BINARY") or shutil.which("da-guard")
    assert exe, "no da-guard to ask"
    proc = subprocess.run([exe, "parse-tree", "--config-dir", str(conf_d)],
                          capture_output=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    doc = json.loads(proc.stdout)
    return sorted(e["path"] for e in doc["entries"] if e["verdict"] is not None)


def assert_refused(rc: int, stderr: str, paths: "list[str]", *, code: int = 2) -> None:
    """The run ended through the shared refusal: exit *code*, every path of
    *paths* named on its own `ERROR:` line, no traceback."""
    assert paths, "the tree has no path Go names: nothing to refuse"
    assert rc == code, (rc, stderr[-2000:])
    assert "Traceback" not in stderr, stderr[-2000:]
    for p in paths:
        assert f"ERROR: {p}:" in stderr, (p, stderr[-2000:])
    assert "da-guard validation names; this tool cannot read its whole input" in stderr, stderr[-2000:]


def write_tree(root: Path, files: "dict[str, str | bytes]") -> Path:
    """*files* (root-relative path → text or bytes) under *root*."""
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return root


@contextlib.contextmanager
def refuses(*paths: str, code: int = 2):
    """The block ends the run through the shared refusal, naming *paths*
    (`assert_refused`) — for an in-process call of a tool's function."""
    err = io.StringIO()
    got = None
    with contextlib.redirect_stderr(err):
        try:
            yield err
        except SystemExit as exc:
            got = exc.code
    assert got is not None, f"no refusal; stderr: {err.getvalue()[-2000:]}"
    assert_refused(got, err.getvalue(), list(paths), code=code)
