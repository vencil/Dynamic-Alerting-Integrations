"""lint_jsx_babel's node pass, through the real subprocess path (#3611).

The Babel parse runs in node. Before #3611 every way it could fail to run —
no node, @babel/standalone not installable, a node crash, output that was not
the results — was a "⚠ skipping" and exit 0 without --ci. Now each is no
verdict, exit 2, with or without --ci; and a node run that returns fewer
results than the files it was handed is a run that did not cover them.

The wrapper is run as a process with PATH holding a stand-in node (a Python
script answering the driver's stdin protocol) and TMPDIR pre-seeded with an
``@babel/standalone`` directory, so no npm and no network are involved.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "lint" / "lint_jsx_babel.py"

#: mode: "ok" one ok result per file · "fail-first" first file a parse error ·
#: "short" one result fewer than files · "doubled" first file twice, last
#: missing · "crash" exit 1 · "garbage" not JSON
_STAND_IN_NODE = f"""#!{sys.executable}
import json, os, sys
mode = os.environ["STAND_IN_MODE"]
if mode == "crash":
    print("node: internal error", file=sys.stderr); sys.exit(1)
if mode == "garbage":
    print("not json"); sys.exit(0)
files = json.load(sys.stdin)
out = [{{"path": f["path"], "ok": True}} for f in files]
if mode == "fail-first":
    out[0] = {{"path": files[0]["path"], "ok": False, "error": "Unexpected token"}}
if mode == "short":
    out = out[:-1]
if mode == "doubled":
    out = [out[0]] + out[:-1]
sys.stdout.write(json.dumps(out))
"""


def _run(tmp_path, *, ci, mode=None, babel=True):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if mode is not None:
        node = bin_dir / "node"
        node.write_text(_STAND_IN_NODE, encoding="utf-8", newline="\n")
        node.chmod(0o755)
    tmp = tmp_path / "tmp"
    tmp.mkdir(exist_ok=True)
    if babel:
        babel_dir = tmp / "da-babel-lint" / "node_modules" / "@babel" / "standalone"
        babel_dir.mkdir(parents=True, exist_ok=True)
        if babel != "empty-dir":
            (babel_dir / "package.json").write_text(
                '{"version": "7.26.4"}', encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    env.update(PATH=str(bin_dir), TMPDIR=str(tmp), STAND_IN_MODE=mode or "")
    return subprocess.run([sys.executable, str(_SCRIPT), *(["--ci"] if ci else [])],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120, env=env, stdin=subprocess.DEVNULL)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX stand-in for node")


@pytest.mark.parametrize("ci", [True, False], ids=["ci", "dev-box"])
def test_no_node_is_rc_2_with_or_without_ci(tmp_path, ci):
    """It was "⚠ Node.js not found — skipping" and rc 0, --ci or not."""
    proc = _run(tmp_path, ci=ci)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "node is not available" in proc.stderr


@pytest.mark.parametrize("ci", [True, False], ids=["ci", "dev-box"])
def test_babel_not_installable_is_rc_2(tmp_path, ci):
    """No @babel/standalone and no npm on PATH: the parse cannot run (rc 0 before)."""
    proc = _run(tmp_path, ci=ci, mode="ok", babel=False)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not install @babel/standalone" in proc.stderr


@pytest.mark.parametrize("mode", ["crash", "garbage", "short", "doubled"])
@pytest.mark.parametrize("ci", [True, False], ids=["ci", "dev-box"])
def test_a_node_pass_that_did_not_cover_every_file_is_rc_2(tmp_path, ci, mode):
    """A crash, non-JSON output, or one result missing: rc 2. Without --ci
    the first two used to be rc 0, and a short result list passed either way."""
    proc = _run(tmp_path, ci=ci, mode=mode)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "babel-parse did not run" in proc.stderr, proc.stderr


def test_an_interrupted_babel_install_is_reinstalled_not_trusted(tmp_path):
    """An empty @babel/standalone directory (an npm run cut off) is not an
    install: with no npm on PATH it is "could not install", rc 2 — not a
    node `Cannot find module` on every later run (#3611 review F5)."""
    proc = _run(tmp_path, ci=True, mode="ok", babel="empty-dir")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "could not install @babel/standalone" in proc.stderr


def test_no_verdict_still_prints_what_the_node_free_passes_found(tmp_path, monkeypatch):
    """rc 2 (no verdict) does not swallow the static / line-count findings
    computed before the Babel pass (#3611 review F3)."""
    proc = _run(tmp_path, ci=True)
    assert proc.returncode == 2
    assert "static pattern warning(s) also found" in proc.stdout, proc.stdout


def test_a_parse_error_is_a_finding_under_ci_only(tmp_path):
    assert _run(tmp_path, ci=True, mode="fail-first").returncode == 1
    assert _run(tmp_path, ci=False, mode="fail-first").returncode == 0


def test_a_clean_node_pass_is_rc_0(tmp_path):
    """The control: the stand-in route itself reaches a pass."""
    proc = _run(tmp_path, ci=True, mode="ok")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "did not run" not in proc.stderr


def test_exactly_one_hook_runs_the_jsx_lint_in_its_strictest_mode():
    """#3611 B2: one hook, not two. The second (`--ci --strict-linecount`,
    same files:) was a strict superset of the first, so every commit ran the
    Babel pass twice; the remaining hook carries the superset's flags."""
    import yaml
    cfg = yaml.safe_load((_SCRIPT.parents[3] / ".pre-commit-config.yaml").read_text(
        encoding="utf-8"))
    hooks = [h for r in cfg["repos"] for h in r.get("hooks", [])
             if "lint_jsx_babel.py" in h.get("entry", "")]
    assert [h["id"] for h in hooks] == ["jsx-babel-check"], hooks
    assert "--strict-linecount" in hooks[0]["entry"].split()
