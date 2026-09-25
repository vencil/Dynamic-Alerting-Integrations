"""Python half of tests/shared/init_declaration_parity_matrix.json (#1942 F1).

`da-tools init` decides which conf.d file declares which tenant before it
writes anything (`init_project._plan_confd`). Its answer has to be the
exporter's: the Go half (components/threshold-exporter/app/pkg/config/
init_declaration_parity_test.go) runs `ScanDirTree` over every row of the
same table. Neither side reads the other's source.

⚠️ Soundness is the direction that matters and is asserted without slack:
a file init calls accepted must be accepted by the exporter WITH THE SAME
tenant ids, and a file the exporter rejects must never be called accepted.
Only a row's `init_unsure` files may be answered "cannot tell" — init then
refuses when such a file names a requested tenant (fail-closed).
"""
from __future__ import annotations

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
_TREE_KEYS = {"name", "files", "declarations", "rejected", "conflict",
              "init_unsure"}


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
    # the blind-review shapes this table exists for
    assert sum(n.startswith("F1-1a-") for n in names) >= 4
    assert sum(n.startswith("F1-1b-") for n in names) >= 2


def _config() -> dict:
    # A tenant no row mentions: the plan's classification of the tree is
    # what is asserted, not a skip/refuse decision about it.
    return {"ci": "github", "deploy": "kustomize", "rule_packs": ["mariadb"],
            "tenants": ["zz-parity-probe"], "namespace": "monitoring",
            "da_tools_image": ip.DA_TOOLS_IMAGE}


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_init_classifies_the_tree_as_the_exporter_does(tree, tmp_path) -> None:
    conf = tmp_path / "conf.d"
    for rel, content in tree["files"].items():
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8"))

    plan = ip._plan_confd(_config(), str(tmp_path))

    unsure = set(tree["init_unsure"])
    judged = {f[len("conf.d/"):]: v for f, v in plan.unreadable.items()}
    # 1. a file the exporter rejects is never read as declaring anyone
    for rel in tree["rejected"]:
        assert rel in judged, f"{rel}: exporter rejects it, init accepted it"
        if rel not in unsure:
            assert judged[rel][0] == "reject", (rel, judged[rel])
    # 2. a file the exporter accepts: same ids, or (listed) 'cannot tell'
    for rel in tree["declarations"]:
        if rel in judged:
            assert rel in unsure and judged[rel][0] == "unsure", (
                f"{rel}: exporter accepts it, init says {judged[rel]}")
    assert set(judged) <= set(tree["rejected"]) | unsure, judged
    want: dict[str, list[str]] = {}
    for rel, ids in tree["declarations"].items():
        if rel in judged:
            continue
        for tid in ids:
            want.setdefault(tid, []).append(f"conf.d/{rel}")
    got = {t: sorted(fs) for t, fs in plan.declared.items()}
    assert got == {t: sorted(fs) for t, fs in want.items()}
    # 3. the whole-tree verdict: a tenant in two accepted files
    dup = sorted(t for t, fs in plan.declared.items() if len(fs) > 1)
    assert dup == ([tree["conflict"]] if tree["conflict"] else [])
