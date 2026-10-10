"""#2845 — every repo-data lookup takes THIS tree's root, never an outer project's.

The lookups used to stop at the first ancestor carrying a project marker
(``.git`` / ``Makefile`` / ``pyproject.toml``). A tree with no marker at its
own root — a vendored copy, an rsync'd ``scripts/`` — then adopted the
enclosing project's files. Measured before the fix: with an outer repo holding
a plausible but different ``tenant-config.schema.json``, ``validate_config``
and ``generate_alertmanager_routes --validate`` passed an invalid webhook URL
with rc 0, and ``describe_tenant`` described the outer repo's ``conf.d``.

Two layouts, both built outside this repo under an outer project that has a
marker and a decoy copy of every file the lookups read:

* ``bare``: only ``scripts/`` is vendored. Every lookup must come back empty,
  never with a decoy path. What the callers then do is unchanged by #2845:
  ``validate_config`` fails with rc 2, ``generate_alertmanager_routes
  --validate`` raises (a traceback, rc 1), as they do with no tree at all.
* ``own``: the vendored tree also carries its data but no marker (a
  ``git archive`` minus its root files). Every lookup must find THAT data —
  the must-fire control that keeps "returns None" from passing for free.
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

# Every file a lookup reads. The decoys are real copies (a plausible other
# version of this platform), except VERSION, which must still parse.
DECOY_VERSION = "0.0.1"
DATA = [
    "docs/schemas/tenant-config.schema.json",
    "docs/schemas/platform-defaults.schema.json",
    "docs/schemas/routing-profiles.schema.json",
    "rule-packs/rule-pack-mariadb.yaml",
    "k8s/03-monitoring/configmap-rules-platform.yaml",
    "components/da-tools/app/VERSION",
]

PROBE = r'''
import json, sys
root = sys.argv[1]
for sub in ("", "/ops", "/dx", "/lint"):
    sys.path.insert(0, root + "/scripts/tools" + sub)
import validate_config, _threshold_alerts, _lib_validation, _registry_lib
import _observed_map_lib, generate_alertmanager_routes, _grar_validate
s = lambda p: None if p is None else str(p)
print(json.dumps({
    "validate_config._find_schema": s(validate_config._find_schema(
        "tenant-config.schema.json")),
    "_threshold_alerts.find_rule_pack_paths": s(
        (_threshold_alerts.find_rule_pack_paths() or [None])[0]),
    "_lib_validation._find_tenant_schema": s(
        _lib_validation._find_tenant_schema()),
    "_registry_lib._REPO_ROOT": _registry_lib._REPO_ROOT,
    "_observed_map_lib.DEFAULT_RULE_PACKS_DIR": s(
        _observed_map_lib.DEFAULT_RULE_PACKS_DIR),
    "generate_alertmanager_routes.da_tools_version":
        generate_alertmanager_routes.da_tools_version(),
    "_grar_validate._find_platform_rules_configmap": s(
        _grar_validate._find_platform_rules_configmap()),
}))
'''


def _vendor(tmp: Path, own_data: bool) -> tuple[Path, Path]:
    outer = tmp / "outer"
    # Vendored under the outer project's own scripts/tools: "the scripts/tools
    # we sit in" then has two answers, and only the innermost is ours.
    tree = outer / "scripts" / "tools" / "third_party" / "da"
    (outer / "Makefile").parent.mkdir(parents=True)
    (outer / "Makefile").write_text("", encoding="utf-8")
    for rel in DATA:
        d = outer / rel
        d.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith("VERSION"):
            d.write_text(DECOY_VERSION + "\n", encoding="utf-8")
        else:
            shutil.copy2(REPO_ROOT / rel, d)
    (outer / "conf.d").mkdir()
    (outer / "conf.d" / "decoy.yaml").write_text(
        "tenants:\n  decoy-tenant:\n    decoy_key: '1'\n", encoding="utf-8")
    ls = subprocess.run(["git", "ls-files", "-z", "scripts"], cwd=REPO_ROOT,
                        capture_output=True, check=True, timeout=60)
    for rel in filter(None, ls.stdout.decode("utf-8").split("\0")):
        src = REPO_ROOT / rel
        if src.is_file():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tree / rel)
    if own_data:
        for rel in DATA:
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, tree / rel)
    return outer, tree


def _probe(tree: Path, cwd: Path) -> dict:
    r = subprocess.run([sys.executable, "-I", "-c", PROBE, str(tree)],
                       cwd=str(cwd), capture_output=True, timeout=180,
                       env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-2000:]
    return json.loads(r.stdout.decode("utf-8").strip().splitlines()[-1])


@pytest.fixture(scope="module")
def layouts(tmp_path_factory):
    out = {}
    for name, own in (("bare", False), ("own", True)):
        out[name] = _vendor(tmp_path_factory.mktemp(name), own)
    return out


def test_a_bare_vendored_tree_adopts_nothing_from_the_outer_project(layouts):
    outer, tree = layouts["bare"]
    got = _probe(tree, outer)
    adopted = {k: v for k, v in got.items()
               if v and (str(outer) in v or v == DECOY_VERSION)
               and not v.startswith(str(tree))}
    assert not adopted, f"took the outer project's files: {adopted}"


def test_a_vendored_tree_with_its_own_data_finds_it(layouts):
    """Must-fire: no marker at its root, data present — it is used."""
    outer, tree = layouts["own"]
    got = _probe(tree, outer)
    wrong = {k: v for k, v in got.items()
             if not v or not (v.startswith(str(tree)) or v[0].isdigit())}
    assert not wrong, f"did not find this tree's own data: {wrong}"
    assert got["generate_alertmanager_routes.da_tools_version"] == (
        (REPO_ROOT / "components/da-tools/app/VERSION")
        .read_text(encoding="utf-8").strip())


def test_describe_tenant_never_describes_the_outer_projects_conf_d(layouts):
    outer, tree = layouts["bare"]
    r = subprocess.run(
        [sys.executable, str(tree / "scripts/tools/dx/describe_tenant.py"),
         "decoy-tenant"], cwd=str(outer), capture_output=True, timeout=120,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    assert str(outer / "conf.d") + ":" not in out and "Scanned " + str(
        outer / "conf.d") not in out, out[-600:]
    assert "--conf-d" in out, out[-600:]   # names the remedy


def test_the_checkout_still_resolves_to_this_repo(tmp_path):
    got = _probe(REPO_ROOT, tmp_path)
    off = {k: v for k, v in got.items()
           if not v or not (v.startswith(str(REPO_ROOT)) or v[0].isdigit())}
    assert not off, off


def test_the_not_found_messages_name_the_rule_that_applies(monkeypatch):
    """A marker no longer decides the root, so the remedy must not say add one."""
    import importlib  # noqa: PLC0415
    oml = importlib.import_module("_observed_map_lib")
    reg = importlib.import_module("_registry_lib")
    monkeypatch.setattr(oml, "DEFAULT_RULE_PACKS_DIR", None)
    with pytest.raises(oml.RulePacksNotFoundError) as packs:
        oml.default_pack_paths()
    with pytest.raises(reg.RepoTreeNotFoundError) as tree:
        reg._repo_path_or_raise(None, "the thing")
    for msg in (str(packs.value), str(tree.value)):
        assert "scripts/tools" in msg, msg
        assert "Makefile" not in msg and "pyproject" not in msg, msg
