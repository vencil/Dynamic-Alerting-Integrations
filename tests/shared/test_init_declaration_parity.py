"""Python half of tests/shared/init_declaration_parity_matrix.json (#1942).

`da-tools init` decides which conf.d file declares which tenant before it
writes anything (`init_project._plan_confd`). The Go half
(components/threshold-exporter/app/pkg/config/init_declaration_parity_test.go)
runs the exporter's `ScanDirTree` over every row of the same table. Neither
side reads the other's source.

⛔ The property this file exists for: init's errors may only go ONE way.
It may refuse a file the exporter would accept (a "conservative
over-refusal": the file is outside init's whitelist, init says it cannot
tell). It must never
  * call a file accepted that the exporter rejects (the tenant it skipped
    for that file then exists nowhere), nor
  * call a file accepted with different tenant ids than the exporter reads
    (a declaration it does not see becomes a duplicate).
`test_errors_only_ever_go_the_conservative_way` counts the second kind
across the whole table and requires zero; the first kind has to match
`KNOWN_CONSERVATIVE` exactly, so a new one is a reviewed decision.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

import init_project as ip  # noqa: E402

MATRIX_PATH = Path(__file__).parent / "init_declaration_parity_matrix.json"
MATRIX = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
_TREE_KEYS = {"name", "files", "files_base64", "declarations", "rejected",
              "conflict", "init_unsure"}

#: (row, file) where the exporter ACCEPTS the file and init says it cannot
#: tell — the price of the whitelist (`_whitelist_violation`), paid as a
#: refusal whenever such a file names a requested tenant.
KNOWN_CONSERVATIVE = {
    ("F1-1a-key-on-is-a-string", "team.yaml"),       # `on:` is bool in YAML 1.1
    ("F1-1a-key-007-keeps-its-text", "team.yaml"),   # `007:` is int in YAML 1.1
    ("R2-merge-key-and-anchor", "team.yaml"),
    ("R2-explicit-tag-on-a-value", "team.yaml"),
    ("R2-local-tag", "team.yaml"),
    ("R2-trailing-tab", "team.yaml"),
    ("R2-tab-in-flow-mapping", "team.yaml"),
    ("R2-json-with-tab", "team.yaml"),
    ("R2-utf16le-with-bom", "team.yaml"),
    ("R2-timestamp-value", "team.yaml"),
}


def test_matrix_shape_is_exactly_what_both_readers_decode() -> None:
    """Python's DisallowUnknownFields: a misspelt key would be ignored."""
    assert set(MATRIX) == {"_comment", "trees"}
    for tree in MATRIX["trees"]:
        assert set(tree) == _TREE_KEYS, (tree.get("name"), set(tree))


def test_matrix_is_not_vacuous() -> None:
    names = [t["name"] for t in MATRIX["trees"]]
    assert len(names) == len(set(names))
    assert any(t["rejected"] for t in MATRIX["trees"])
    assert any(t["conflict"] for t in MATRIX["trees"])
    assert any(t["init_unsure"] for t in MATRIX["trees"])
    assert any(t["files_base64"] for t in MATRIX["trees"])
    # the blind-review shapes this table exists for
    assert sum(n.startswith("F1-1a-") for n in names) >= 4
    assert sum(n.startswith("F1-1b-") for n in names) >= 2
    assert sum(n.startswith("R2-") for n in names) >= 6
    assert {r for r, _ in KNOWN_CONSERVATIVE} <= set(names)


def _config(tenants: list[str]) -> dict:
    return {"ci": "github", "deploy": "kustomize", "rule_packs": ["mariadb"],
            "tenants": tenants, "namespace": "monitoring",
            "da_tools_image": ip.DA_TOOLS_IMAGE}


def _build(tree: dict, base: Path) -> None:
    conf = base / "conf.d"
    blobs = {rel: c.encode("utf-8") for rel, c in tree["files"].items()}
    blobs.update({rel: base64.b64decode(b)
                  for rel, b in tree["files_base64"].items()})
    for rel, data in blobs.items():
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _classify(tree: dict, base: Path):
    """init's answer for the tree: (plan, {file: status}) — a tenant no row
    mentions is requested, so no skip/refuse decision muddies it."""
    _build(tree, base)
    plan = ip._plan_confd(_config(["zz-parity-probe"]), str(base))
    judged = {f[len("conf.d/"):]: v[0] for f, v in plan.unreadable.items()}
    return plan, judged


def _violations(tree: dict, base: Path):
    """(wrong-direction errors, conservative over-refusals) for one row."""
    plan, judged = _classify(tree, base)
    wrong, conservative = [], set()
    for rel in tree["rejected"]:
        if rel not in judged:
            wrong.append(f"{rel}: exporter rejects it, init accepted it")
    for rel in tree["declarations"]:
        if rel in judged:
            if judged[rel] == "reject":
                wrong.append(f"{rel}: exporter accepts it, init says reject")
            conservative.add((tree["name"], rel))
    for rel in set(judged) - set(tree["rejected"]) - set(tree["declarations"]):
        wrong.append(f"{rel}: init judged a file the exporter never reads")
    want: dict[str, list[str]] = {}
    for rel, ids in tree["declarations"].items():
        if rel in judged:
            continue
        for tid in ids:
            want.setdefault(tid, []).append(f"conf.d/{rel}")
    got = {t: sorted(fs) for t, fs in plan.declared.items()}
    if got != {t: sorted(fs) for t, fs in want.items()}:
        wrong.append(f"declarations {got} != exporter's {want}")
    dup = sorted(t for t, fs in plan.declared.items() if len(fs) > 1)
    if dup != ([tree["conflict"]] if tree["conflict"] else []):
        wrong.append(f"duplicate verdict {dup} != exporter's {tree['conflict']}")
    return wrong, conservative


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_init_classifies_the_tree_as_the_exporter_does(tree, tmp_path) -> None:
    wrong, _ = _violations(tree, tmp_path)
    assert not wrong, wrong
    _, judged = _classify(tree, tmp_path / "again")
    for rel in tree["init_unsure"]:
        assert rel in judged, f"{rel} is outside the whitelist, init accepted it"


def test_errors_only_ever_go_the_conservative_way(tmp_path) -> None:
    """THE property: zero wrong-direction rows, and the over-refusals are
    exactly the reviewed list."""
    wrong_total, conservative = [], set()
    for i, tree in enumerate(MATRIX["trees"]):
        wrong, cons = _violations(tree, tmp_path / str(i))
        wrong_total += [f"{tree['name']}: {w}" for w in wrong]
        conservative |= cons
    assert wrong_total == [], wrong_total
    assert conservative == KNOWN_CONSERVATIVE, (
        f"new over-refusals: {sorted(conservative - KNOWN_CONSERVATIVE)}; "
        f"no longer over-refused: {sorted(KNOWN_CONSERVATIVE - conservative)}")


@pytest.mark.parametrize(
    "tree", [t for t in MATRIX["trees"] if t["init_unsure"]],
    ids=lambda t: t["name"])
def test_an_unsure_file_naming_a_requested_tenant_refuses(tree, tmp_path):
    _build(tree, tmp_path)
    for rel, tenant in tree["init_unsure"].items():
        plan = ip._plan_confd(_config([tenant]), str(tmp_path))
        hits = [c for c in plan.conflicts
                if c.kind == "unreadable" and c.tenant == tenant
                and c.files == [f"conf.d/{rel}"]]
        assert hits, (rel, tenant, plan.conflicts)
        assert tenant not in plan.generate and tenant not in plan.skipped
