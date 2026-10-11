"""#2852 — dev-time tools read THIS tree, never an outer project's.

Seventeen tools under ``scripts/tools/{dx,lint}`` took the nearest ancestor
holding a ``.git`` as the repo root — thirteen from their own file, four from
the current directory. A tree with no ``.git`` of its own (a
vendored copy, a ``git archive`` tarball) nested in another project then read
— and the generators wrote into — THAT project. They now take
``_lib_compat.require_project_root``: the root of the innermost
``scripts/tools`` they sit in (#2845's rule), rc 2 outside any.

Layouts, built outside this repo under an outer project carrying a ``.git``
and a decoy copy of every file the two whole-run checks below read:

* ``bare``: only ``scripts/tools`` is vendored. Every root must be the
  vendored tree; the two checks that verify their inputs exist must stop
  with rc 2 instead of scanning the decoys.
* ``own``: the vendored tree also carries those inputs — they are used (the
  must-fire control).
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
TOOLS = REPO_ROOT / "scripts" / "tools"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "lint"))

# (module, sub-directory) for every tool whose root is a `_repo_root()`.
ROOT_FNS = [
    ("gen_am_duration_json", "dx"),
    ("gen_recipe_status_json", "dx"),
    ("gen_tenant_id_json", "dx"),
    ("generate_rulepack_configmaps", "dx"),
    ("check_ha_threshold_aggregation", "lint"),
    ("check_ksm_version_allowlist", "lint"),
    ("check_leftouterjoin_enrichment", "lint"),
    ("check_portal_rulepack_claims", "lint"),
    ("check_retire_drift", "lint"),
    ("check_unpinned_deps", "lint"),
    ("check_workflow_git_push_permissions", "lint"),
]
# Tools whose root used to be the CWD's nearest `.git`; the probe runs from
# inside the outer project, which is exactly what used to pick it.
CWD_FNS = [
    ("bump_playbook_versions", "dx"),
    ("check_devrules_size", "lint"),
    ("check_path_metadata_consistency", "lint"),
    ("check_playbook_freshness", "lint"),
]
# Tools that find the root inline in main(), with the inputs they require.
import check_cross_ns_url_consistency as _cross_ns  # noqa: E402
import check_single_writer_invariant as _single_writer  # noqa: E402

WHOLE_RUN = {
    "check_cross_ns_url_consistency": [str(p) for p in _cross_ns._FIXED_TARGETS],
    "check_single_writer_invariant": [str(p) for p in _single_writer._TARGETS],
}
# What each prints when its inputs are missing — matched exactly, because a
# looser "not found" also matches ordinary output (an exemption's prose).
MISSING = {
    "check_cross_ns_url_consistency": "ERROR: fixed scan target(s) not found:",
    "check_single_writer_invariant": "ERROR: tenant-api Deployment source(s) not found:",
}

PROBE = r'''
import importlib, json, sys
tools = sys.argv[1]
for sub in ("", "/dx", "/lint"):
    sys.path.insert(0, tools + sub)
out = {}
for spec in sys.argv[2:]:
    name, fn = spec.split(":")
    out[name] = str(getattr(importlib.import_module(name), fn)())
print(json.dumps(out))
'''


def _vendor(tmp: Path, own_data: bool) -> tuple[Path, Path]:
    outer = tmp / "outer"
    tree = outer / "scripts" / "tools" / "third_party" / "da"
    (outer / ".git").mkdir(parents=True)  # the marker the old lookup stopped at
    for rel in (r for rels in WHOLE_RUN.values() for r in rels):
        (outer / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, outer / rel)
        if own_data:
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, tree / rel)
    ls = subprocess.run(["git", "ls-files", "-z", "scripts/tools"], cwd=REPO_ROOT,
                        capture_output=True, check=True, timeout=60)
    for rel in filter(None, ls.stdout.decode("utf-8").split("\0")):
        src = REPO_ROOT / rel
        if src.is_file():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tree / rel)
    return outer, tree


def _env() -> dict:
    return dict(os.environ, PYTHONDONTWRITEBYTECODE="1")


@pytest.fixture(scope="module")
def layouts(tmp_path_factory):
    return {name: _vendor(tmp_path_factory.mktemp(name), own)
            for name, own in (("bare", False), ("own", True))}


def _roots(tools: Path, cwd: Path) -> dict:
    r = subprocess.run([sys.executable, "-I", "-c", PROBE, str(tools),
                        *(f"{m}:_repo_root" for m, _ in ROOT_FNS),
                        *(f"{m}:find_repo_root" for m, _ in CWD_FNS)],
                       cwd=str(cwd), capture_output=True, timeout=180, env=_env())
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


def test_every_root_is_the_vendored_tree(layouts):
    outer, tree = layouts["bare"]
    got = _roots(tree / "scripts" / "tools", outer)
    wrong = {k: v for k, v in got.items() if v != str(tree)}
    assert not wrong, f"not this tree's root: {wrong}"


def test_the_checkout_still_resolves_to_this_repo(tmp_path):
    got = _roots(TOOLS, tmp_path)
    wrong = {k: v for k, v in got.items() if v != str(REPO_ROOT)}
    assert not wrong, wrong


def _run(tree: Path, cwd: Path, name: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(tree / "scripts/tools/lint" / f"{name}.py")],
                       cwd=str(cwd), capture_output=True, timeout=180, env=_env())
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


@pytest.mark.parametrize("name", sorted(WHOLE_RUN))
def test_a_bare_tree_never_scans_the_outer_projects_files(layouts, name):
    outer, tree = layouts["bare"]
    rc, out = _run(tree, outer, name)
    assert rc == 2 and MISSING[name] in out, f"rc={rc}\n{out[-800:]}"


@pytest.mark.parametrize("name", sorted(WHOLE_RUN))
def test_a_tree_with_its_own_inputs_scans_them(layouts, name):
    """Must-fire: the same run with the inputs in the tree gets past the check."""
    outer, tree = layouts["own"]
    rc, out = _run(tree, outer, name)
    # rc 0, not "0 or 1": a traceback also exits 1.
    assert rc == 0 and MISSING[name] not in out, f"rc={rc}\n{out[-800:]}"


def test_outside_any_scripts_tools_tree_is_a_caller_error(tmp_path, capsys):
    from _lib_compat import require_project_root  # noqa: PLC0415
    with pytest.raises(SystemExit) as exc:
        require_project_root(tmp_path)
    assert exc.value.code == 2
    assert "scripts/tools" in capsys.readouterr().err
    inner = tmp_path / "scripts" / "tools" / "lint"
    inner.mkdir(parents=True)
    assert require_project_root(inner) == tmp_path.resolve()
