"""Tests for gen_recipe_contract (#3229).

The recipe contract is written once, in the exporter's
pkg/config/recipe_contract.json; the schema members, the portal's two JSON
files, _lib_constants.py's GENERATED block and rule-packs/recipes/*.yaml are generated
from it. These pin that the committed copies are in sync, that a contract edit
reaches every copy, that a hand edit to a copy is caught, and that a malformed
contract or target is a caller error (rc 2), not a silent pass.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import gen_recipe_contract as gen  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "tools" / "dx" / "gen_recipe_contract.py"


def _tree(tmp_path: Path) -> Path:
    """A minimal copy of every file the generator reads or writes."""
    for rel in (gen.CONTRACT_REL, gen.SEVERITY_REL, gen.SCHEMA_REL, gen.PORTAL_ENUMS_REL,
                gen.PORTAL_STATUS_REL, gen.CONSTANTS_REL, gen.POLICY_REL, *gen.GOVERNANCE_DOC_RELS):
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_REPO / rel, dst)
    shutil.copytree(_REPO / gen.RECIPES_DIR_REL, tmp_path / gen.RECIPES_DIR_REL,
                    ignore=shutil.ignore_patterns("examples"))
    return tmp_path


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def _edit_contract(root: Path, fn) -> None:
    path = root / gen.CONTRACT_REL
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def test_committed_copies_are_in_sync():
    assert gen.run(_REPO, update=False) == []


def test_committed_copies_are_lf_on_disk():
    # esbuild bundles the portal copies from the raw bytes; --check reads with
    # universal newlines and cannot see a CRLF copy.
    for rel in (gen.PORTAL_ENUMS_REL, gen.PORTAL_STATUS_REL):
        assert b"\r\n" not in (_REPO / rel).read_bytes(), rel


def test_a_contract_edit_reaches_every_copy(tmp_path):
    root = _tree(tmp_path)

    def add(doc):
        doc["horizon"]["enum"].append("72h")
        doc["recipes"][1]["status"] = "deprecated"   # rate

    _edit_contract(root, add)
    r = _cli(root, "--check")
    assert r.returncode == 1, r.stdout + r.stderr
    for rel in (gen.SCHEMA_REL, gen.PORTAL_ENUMS_REL, gen.PORTAL_STATUS_REL, gen.CONSTANTS_REL,
                f"{gen.RECIPES_DIR_REL}/forecast.yaml", f"{gen.RECIPES_DIR_REL}/rate.yaml",
                gen.POLICY_REL, *gen.GOVERNANCE_DOC_RELS):
        assert f"STALE: {rel}" in r.stdout
    assert _cli(root, "--update").returncode == 0
    assert _cli(root, "--check").returncode == 0

    schema = json.loads((root / gen.SCHEMA_REL).read_text(encoding="utf-8"))
    props = schema["definitions"]["customAlertInstance"]["properties"]
    assert "72h" in props["horizon"]["enum"]
    assert "72h" in json.loads((root / gen.PORTAL_ENUMS_REL).read_text(encoding="utf-8"))["horizon"]
    assert json.loads((root / gen.PORTAL_STATUS_REL).read_text(encoding="utf-8"))["statuses"]["rate"] == "deprecated"
    assert 'FORECAST_MAX_LOOKBACK: Final[str] = "144h"' in (root / gen.CONSTANTS_REL).read_text(encoding="utf-8")
    forecast = yaml.safe_load((root / gen.RECIPES_DIR_REL / "forecast.yaml").read_text(encoding="utf-8"))
    assert "72h" in forecast["params"]["horizon"]["enum"]
    rate = yaml.safe_load((root / gen.RECIPES_DIR_REL / "rate.yaml").read_text(encoding="utf-8"))
    assert rate["status"] == "deprecated"
    # 72h is now the longest horizon: the compiled pack reads 2 × 72h.
    policy = yaml.safe_load((root / gen.POLICY_REL).read_text(encoding="utf-8"))
    assert policy["file_overrides"][0]["policy"]["max_range_duration"] == "144h"
    for rel in gen.GOVERNANCE_DOC_RELS:
        assert "max_range_duration: 144h " in (root / rel).read_text(encoding="utf-8"), rel


@pytest.mark.parametrize("edit, want", [
    (lambda f: f.update(lookback_factor=3), "144h"),     # 3 × 48h
    (lambda f: f.update(lookback_floor="100h"), "100h"),  # the floor outgrows 2 × 48h
])
def test_the_forecast_lookback_reaches_the_policy(tmp_path, edit, want):
    root = _tree(tmp_path)
    _edit_contract(root, lambda d: edit(d["forecast"]))
    assert _cli(root, "--update").returncode == 0
    policy = yaml.safe_load((root / gen.POLICY_REL).read_text(encoding="utf-8"))
    assert policy["file_overrides"][0]["policy"]["max_range_duration"] == want
    assert f'FORECAST_MAX_LOOKBACK: Final[str] = "{want}"' in (root / gen.CONSTANTS_REL).read_text(encoding="utf-8")


def test_a_hand_edited_copy_is_caught(tmp_path):
    root = _tree(tmp_path)
    p = root / gen.PORTAL_ENUMS_REL
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["forecastCurrentBand"] = 1
    p.write_text(json.dumps(doc), encoding="utf-8")
    r = _cli(root, "--check")
    assert r.returncode == 1
    assert f"STALE: {gen.PORTAL_ENUMS_REL}" in r.stdout


def test_op_enum_is_per_recipe():
    # `==` is threshold-only: the governance yamls offer it there and nowhere else.
    for path in sorted((_REPO / gen.RECIPES_DIR_REL).glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        op = doc["params"].get("op")
        if op is None:
            continue
        assert ("==" in op["enum"]) == (doc["recipe"] == "threshold"), path.name


def test_governance_yaml_status_and_enums_match_the_contract():
    c = json.loads((_REPO / gen.CONTRACT_REL).read_text(encoding="utf-8"))
    for r in c["recipes"]:
        doc = yaml.safe_load((_REPO / gen.RECIPES_DIR_REL / f"{r['name']}.yaml").read_text(encoding="utf-8"))
        assert doc["status"] == r["status"]
        mode = doc["params"]["mode"]
        assert mode["enum"] == c["mode"]["enum"] and mode["default"] == c["mode"]["default"]
        assert doc["params"]["for"]["enum"] == c["for"]["enum"]


@pytest.mark.parametrize("case", ["schema_extra_property", "missing_yaml", "unknown_param",
                                  "undocumented_param", "bad_json", "zero_lookback_factor",
                                  "bad_lookback_floor", "no_lookback_line",
                                  "lookback_comment_on_its_own_line"])
def test_malformed_inputs_are_caller_errors(tmp_path, case):
    root = _tree(tmp_path)
    if case == "schema_extra_property":
        _edit_contract(root, lambda d: d["fields"].append({"name": "lookback"}))
    elif case == "missing_yaml":
        (root / gen.RECIPES_DIR_REL / "ratio.yaml").unlink()
    elif case == "undocumented_param":
        p = root / gen.RECIPES_DIR_REL / "rate.yaml"
        lines = [ln for ln in p.read_text(encoding="utf-8").split("\n") if not ln.startswith("  op:")]
        p.write_text("\n".join(lines), encoding="utf-8")
    elif case == "unknown_param":
        p = root / gen.RECIPES_DIR_REL / "rate.yaml"
        p.write_text(p.read_text(encoding="utf-8").replace(
            "  window:", "  lookback:  {required: false, desc: \"x\"}\n  window:", 1), encoding="utf-8")
    elif case == "zero_lookback_factor":
        _edit_contract(root, lambda d: d["forecast"].update(lookback_factor=0))
    elif case == "bad_lookback_floor":
        _edit_contract(root, lambda d: d["forecast"].update(lookback_floor="1h30m"))
    elif case == "lookback_comment_on_its_own_line":
        # The comment must be on the value's line: a bare value followed by a
        # comment line is not the generated line (it could be another limit).
        p = root / gen.POLICY_REL
        p.write_text(p.read_text(encoding="utf-8").replace(
            "96h   # forecast lookback", "96h\n      # forecast lookback", 1), encoding="utf-8")
    elif case == "no_lookback_line":
        p = root / gen.POLICY_REL
        p.write_text(p.read_text(encoding="utf-8").replace("# forecast lookback", "#"), encoding="utf-8")
    else:
        (root / gen.CONTRACT_REL).write_text("{", encoding="utf-8")
    r = _cli(root, "--check")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "ERROR:" in r.stderr


def test_the_precommit_hook_sees_every_input_and_target():
    # Derived from the generator's own target list, so a new target cannot be
    # left outside the hook's `files` (the hook would then never run for it).
    cfg = yaml.safe_load((_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [h for repo in cfg["repos"] for h in repo.get("hooks", []) if h.get("id") == "recipe-contract-check"]
    assert len(hooks) == 1
    files = re.compile(hooks[0]["files"])
    c, severities = gen.load(_REPO)
    rels = [rel for rel, _ in gen.targets(_REPO, c, severities)]
    rels += [gen.CONTRACT_REL, gen.SEVERITY_REL, "scripts/tools/dx/gen_recipe_contract.py"]
    missed = [rel for rel in rels if not files.search(rel)]
    assert not missed, f"recipe-contract-check `files` misses {missed}"


def test_eq_recipes_reach_the_schema(tmp_path):
    # The review's mutation: widening eq_recipes used to leave the schema's
    # hand-written `== -> threshold` rule untouched while --check stayed green.
    root = _tree(tmp_path)
    _edit_contract(root, lambda d: d["op"]["eq_recipes"].append("rate"))
    r = _cli(root, "--check")
    assert f"STALE: {gen.SCHEMA_REL}" in r.stdout, r.stdout
    assert _cli(root, "--update").returncode == 0
    schema = json.loads((root / gen.SCHEMA_REL).read_text(encoding="utf-8"))
    eq = [a for a in schema["definitions"]["customAlertInstance"]["allOf"]
          if a["if"].get("properties", {}).get("op") == {"const": "=="}]
    assert eq and eq[0]["then"]["properties"]["recipe"]["enum"] == ["threshold", "rate"]


def test_required_fields_reach_schema_and_yaml(tmp_path):
    root = _tree(tmp_path)

    def require_op(d):
        r = next(r for r in d["recipes"] if r["name"] == "rate")
        r["required"].append("op")

    _edit_contract(root, require_op)
    assert _cli(root, "--update").returncode == 0
    schema = json.loads((root / gen.SCHEMA_REL).read_text(encoding="utf-8"))
    rate = [a for a in schema["definitions"]["customAlertInstance"]["allOf"]
            if a["if"].get("properties", {}).get("recipe") == {"const": "rate"}]
    assert "op" in rate[0]["then"]["required"]
    doc = yaml.safe_load((root / gen.RECIPES_DIR_REL / "rate.yaml").read_text(encoding="utf-8"))
    assert doc["params"]["op"]["required"] is True


def test_the_schema_refuses_what_the_go_rules_refuse():
    # One example per allOf rule kind; Go's checkRecipeFields refuses each
    # (pkg/config TestCheckRecipeFields_FromContract).
    import jsonschema

    schema = json.loads((_REPO / gen.SCHEMA_REL).read_text(encoding="utf-8"))
    inst = dict(schema["definitions"]["customAlertInstance"])
    inst["definitions"] = schema["definitions"]
    v = jsonschema.Draft7Validator(inst)
    ok = {"recipe": "ratio", "name": "a", "metric": "m", "denominator_metric": "d",
          "window": "5m", "threshold": "1"}
    assert not list(v.iter_errors(ok))
    for bad in (
        {k: x for k, x in ok.items() if k != "denominator_metric"},
        {**ok, "op": "=="},
        {"recipe": "absence", "name": "a", "metric": "m", "window": "5m", "threshold": "1",
         "group_by": ["persistentvolumeclaim"]},
        {"recipe": "slo_burn_rate", "name": "a", "metric": "m", "denominator_metric": "d",
         "objective": "99.9", "threshold": "1"},
        {"recipe": "slo_burn_rate", "name": "a", "metric": "m", "denominator_metric": "d",
         "objective": "99.9", "op": "<"},
    ):
        assert list(v.iter_errors(bad)), bad


def test_empty_group_by_is_none_in_the_schema():
    # The schema and Go (pkg/config TestEmptyGroupByIsNone) both read
    # `group_by: []` as "none"; a non-empty list on absence is refused by both.
    import jsonschema

    schema = json.loads((_REPO / gen.SCHEMA_REL).read_text(encoding="utf-8"))
    inst = dict(schema["definitions"]["customAlertInstance"])
    inst["definitions"] = schema["definitions"]
    v = jsonschema.Draft7Validator(inst)
    base = {"recipe": "absence", "name": "a", "metric": "m", "window": "5m", "threshold": "1"}
    assert not list(v.iter_errors({**base, "group_by": []}))
    eq = {**base, "recipe": "threshold", "op": "==", "group_by": []}
    assert not list(v.iter_errors(eq))
    pvc = {**base, "group_by": ["persistentvolumeclaim"]}
    assert list(v.iter_errors(pvc))
