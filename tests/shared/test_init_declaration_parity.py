"""Python half of tests/shared/init_declaration_parity_matrix.json (#1942).

`da-tools init` skips any requested tenant another conf.d file POSSIBLY
mentions (`init_project._possible_mentions`); it no longer decides whether
the exporter can read a file (owner ruling, option 3). The Go half
(components/threshold-exporter/app/pkg/config/init_declaration_parity_test.go)
runs the exporter's `ScanDirTree` over every row of the same table. Neither
side reads the other's source.

⛔ The one property that matters, asserted per row and per file:
    tenants the exporter reads from a file  ⊆  tenants init says it may mention
Violated, init generates `conf.d/<t>.yaml` beside a file the exporter
already reads t from — a duplicate, and the exporter rejects the WHOLE
tree. Over-coverage (init skipping a tenant the exporter never reads) is
the price of the rule and is not asserted against.
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
              "conflict"}


def test_matrix_shape_is_exactly_what_both_readers_decode() -> None:
    """Python's DisallowUnknownFields: a misspelt key would be ignored."""
    assert set(MATRIX) == {"_comment", "trees"}
    for tree in MATRIX["trees"]:
        assert set(tree) == _TREE_KEYS, (tree.get("name"), set(tree))


def test_matrix_is_not_vacuous() -> None:
    names = [t["name"] for t in MATRIX["trees"]]
    assert len(names) == len(set(names))
    assert sum(bool(ids) for t in MATRIX["trees"]
               for ids in t["declarations"].values()) >= 20
    assert any(t["rejected"] for t in MATRIX["trees"])
    assert any(t["conflict"] for t in MATRIX["trees"])
    assert any(t["files_base64"] for t in MATRIX["trees"])
    # the blind-review shapes this table exists for
    assert sum(n.startswith("F1-1a-") for n in names) >= 4
    assert sum(n.startswith("R2-") for n in names) >= 6
    assert sum(n.startswith("R3-") for n in names) >= 10
    assert sum(n.startswith("R5-") for n in names) >= 5


def _build(tree: dict, base: Path) -> Path:
    conf = base / "conf.d"
    blobs = {rel: c.encode("utf-8") for rel, c in tree["files"].items()}
    blobs.update({rel: base64.b64decode(b)
                  for rel, b in tree["files_base64"].items()})
    for rel, data in blobs.items():
        p = conf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return conf


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_init_covers_every_tenant_the_exporter_reads(tree, tmp_path) -> None:
    conf = _build(tree, tmp_path)
    for rel, go_ids in tree["declarations"].items():
        got = ip._possible_mentions(conf / rel, sorted(go_ids))
        missed = set(go_ids) - got
        assert not missed, (
            f"{rel}: the exporter reads {sorted(missed)} from it, init does "
            f"not see a possible mention — it would write a duplicate")


#: Rows whose coverage must come from a CONCRETE mention — (a), (b) or (c) —
#: not from (d) "cannot read it through, so it names everyone". (d) would
#: also pass the subset assertion above, which is exactly why these are
#: pinned separately: dropping the UTF-16 decodings or the escaped-line-break
#: handling turned those rows into (d) and the table stayed green (round-5
#: review F3, mutants M4/M6).
CONCRETE_REQUIRED = {
    "R2-utf16le-with-bom",
    "R3-escaped-key-in-flow-with-tab",
    "R5-escaped-LF-break-in-key-with-tab",
    "R5-escaped-CR-break-in-key-with-tab",
    "R5-escaped-NEL-break-in-key-with-tab",
    "R5-escaped-LS-break-in-key-with-tab",
    "R5-escaped-PS-break-in-key-with-tab",
}


@pytest.mark.parametrize(
    "tree", [t for t in MATRIX["trees"] if t["name"] in CONCRETE_REQUIRED],
    ids=lambda t: t["name"])
def test_the_coverage_of_these_rows_is_concrete_not_a_blanket(tree, tmp_path):
    conf = _build(tree, tmp_path)
    for rel, go_ids in tree["declarations"].items():
        got, why = ip._mentions(conf / rel, sorted(go_ids))
        assert why == "", f"{rel}: covered only by (d) — {why}"
        assert set(go_ids) <= got, (rel, go_ids, got)


def test_every_concrete_required_row_exists() -> None:
    assert CONCRETE_REQUIRED <= {t["name"] for t in MATRIX["trees"]}


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_the_plan_never_generates_a_tenant_the_exporter_reads_elsewhere(
        tree, tmp_path) -> None:
    """Same property one level up: through `_plan_confd`, with every tenant
    the exporter reads requested at once."""
    _build(tree, tmp_path)
    wanted = sorted({t for ids in tree["declarations"].values() for t in ids})
    if not wanted:
        pytest.skip("the exporter reads no tenant from this tree")
    plan = ip._plan_confd(
        {"ci": "github", "deploy": "kustomize", "rule_packs": ["mariadb"],
         "tenants": wanted, "namespace": "monitoring",
         "da_tools_image": ip.DA_TOOLS_IMAGE}, str(tmp_path))
    for rel, ids in tree["declarations"].items():
        for t in ids:
            if rel == f"{t}.yaml":
                continue      # init's own path: rewriting it is not a copy
            assert t not in plan.generate, (rel, t, plan)
