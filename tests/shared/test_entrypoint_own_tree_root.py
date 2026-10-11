"""#2852 — da-tools' local-dev fallback runs THIS tree's tools, never an outer project's.

``entrypoint._resolve_script_path`` first looks beside itself (the image's flat
``/opt/da-tools``). In a checkout nothing is there, so it falls back to the
repo's ``scripts/tools/{ops,dx,}``. The repo root used to be the nearest
ancestor with a ``.git``; a copy with no ``.git`` of its own (vendored,
``git archive``) nested in another project then ran THAT project's
``scripts/tools/ops/<tool>.py``.

The root is now taken by structure: ``entrypoint.py`` sits at
``components/da-tools/app``, so the root is three levels up. Anywhere else
(the image) there is no repo to fall back to.

Every layout below sits under an outer project with a ``.git`` and a decoy
``scripts/tools/ops/check_alert.py``:

* ``bare``: only ``components/da-tools/app/entrypoint.py`` is vendored. The
  fallback must find nothing — never the decoy.
* ``own``: the vendored tree also carries its own ``check_alert.py``. It must
  be found (the must-fire control).
* ``flat``: ``entrypoint.py`` alone in a directory that is not
  ``components/da-tools/app`` (an image-like layout missing the tool). Nothing
  must be found.
* ``app``: in an ``app/`` directory, but not ``components/da-tools/app``.
  Three levels up is the outer project, so matching the last part alone would
  run the decoy.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = REPO_ROOT / "components" / "da-tools" / "app" / "entrypoint.py"
APP_REL = Path("components") / "da-tools" / "app"
TOOL = "check_alert.py"

PROBE = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("ep", sys.argv[1])
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)
path, searched = ep._resolve_script_path(sys.argv[2])
print(json.dumps({"path": path, "searched": searched}))
'''


def _stub(path: Path, marker: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"print({marker!r})\n", encoding="utf-8")


def _layout(tmp: Path, kind: str) -> tuple[Path, Path, Path]:
    """Return (outer, tree, the entrypoint.py to run)."""
    outer = tmp / "outer"
    (outer / ".git").mkdir(parents=True)
    _stub(outer / "scripts" / "tools" / "ops" / TOOL, "DECOY")
    tree = outer / "vendor" / "da"
    if kind == "flat":
        ep = tree / "entrypoint.py"
    elif kind == "app":
        ep = tree / "app" / "entrypoint.py"
    else:
        ep = tree / APP_REL / "entrypoint.py"
    ep.parent.mkdir(parents=True)
    shutil.copy2(ENTRYPOINT, ep)
    if kind == "own":
        _stub(tree / "scripts" / "tools" / "ops" / TOOL, "OWN")
    return outer, tree, ep


def _probe(ep: Path, cwd: Path) -> dict:
    r = subprocess.run([sys.executable, "-I", "-c", PROBE, str(ep), TOOL],
                       cwd=str(cwd), capture_output=True, timeout=60,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


@pytest.mark.parametrize("kind", ["bare", "flat", "app"])
def test_no_own_tool_never_falls_back_to_the_outer_project(tmp_path, kind):
    outer, tree, ep = _layout(tmp_path, kind)
    got = _probe(ep, outer)
    assert got["path"] is None, f"ran the outer project's tool: {got['path']}"
    stray = [p for p in got["searched"] if not p.startswith(str(tree))]
    assert not stray, f"searched outside this tree: {stray}"


def test_a_vendored_tree_with_its_own_tool_finds_it(tmp_path):
    """Must-fire: no .git at its root, the tool present — it is used."""
    outer, tree, ep = _layout(tmp_path, "own")
    got = _probe(ep, outer)
    assert got["path"] == str(tree / "scripts" / "tools" / "ops" / TOOL), got


def test_the_dispatcher_does_not_run_the_outer_projects_tool(tmp_path):
    """End to end: rc 2 and the not-found message, never the decoy's output."""
    outer, _tree, ep = _layout(tmp_path, "bare")
    r = subprocess.run([sys.executable, str(ep), "check-alert"], cwd=str(outer),
                       capture_output=True, timeout=60,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
                                DA_LANG="en"))
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert "DECOY" not in out, out[-600:]
    assert r.returncode == 2 and "Tool script not found" in out, out[-600:]


def test_the_checkout_still_finds_its_tools(tmp_path):
    got = _probe(ENTRYPOINT, tmp_path)
    assert got["path"] == str(REPO_ROOT / "scripts" / "tools" / "ops" / TOOL), got
