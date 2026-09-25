"""Python half of tests/shared/platform_tenant_overlay_matrix.json (#1982).

The routing plane (`_grar_parse.load_tenant_tree`, behind generate-routes /
explain-route / `--validate`) must read a root platform file's `tenants:`
block the way the exporter's /metrics plane does: platform value first, the
tenant's own file wins key by key whatever either file is called, a key the
tenant file does not write keeps the platform value, and a platform file
cannot create a tenant. The Go half
(components/threshold-exporter/app/config_platform_tenant_overlay_test.go)
asserts the same table on /metrics.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
from _grar_parse import load_tenant_tree  # noqa: E402
from _lib_confd import overlay_platform_tenants  # noqa: E402

MATRIX = json.loads((Path(__file__).parent / "platform_tenant_overlay_matrix.json")
                    .read_text(encoding="utf-8"))

# Key sets are exact (the #1967 lesson in the sibling matrix): a misspelt key
# read as absent turns a row into one that tests nothing while staying green.
TOP_KEYS = {"_comment", "trees"}
TREE_KEYS = {"name", "files", "expect"}
EXPECT_KEYS = {"metric", "dedup", "group_wait"}


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])
    # Every cell of the owner's measurement is present, each file-name
    # spelling included — dropping one would silently narrow the pin.
    names = {t["name"] for t in MATRIX["trees"]}
    for fname in ("tx.yaml", "TX.yaml", "0tx.yaml"):
        assert f"c2-c5-platform-only-keys-tenant-file-{fname}" in names
        assert f"c3-same-key-both-files-tenant-file-{fname}" in names


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        assert set(tree) == TREE_KEYS, (tree.get("name"), set(tree) ^ TREE_KEYS)
        for tenant, want in tree["expect"].items():
            assert set(want) == EXPECT_KEYS, (tree["name"], tenant, set(want) ^ EXPECT_KEYS)


def _build(tree: dict, root: Path) -> None:
    for rel, content in tree["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_routing_plane_matches_the_table(tree, tmp_path: Path, capsys) -> None:
    _build(tree, tmp_path)
    got = load_tenant_tree(str(tmp_path))
    parsed_tenants = set(got.dedup_configs)
    for tenant, want in tree["expect"].items():
        assert got.dedup_configs.get(tenant) == want["dedup"], (
            tree["name"], tenant, got.dedup_configs)
        assert got.routing_configs.get(tenant, {}).get("group_wait") == want["group_wait"], (
            tree["name"], tenant, got.routing_configs.get(tenant))
    # No tenant the table does not name appears (the orphan rows would
    # otherwise pass by checking only the tenants they list).
    assert parsed_tenants <= set(tree["expect"]), (tree["name"], parsed_tenants)


def test_an_orphan_is_named_with_its_file_and_tenant(tmp_path: Path, capsys) -> None:
    tree = next(t for t in MATRIX["trees"] if t["name"] == "c1-declared-only-in-profiles-file")
    _build(tree, tmp_path)
    load_tenant_tree(str(tmp_path))
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "tenants.tx" in ln]
    assert len(lines) == 1, lines
    assert "_profiles.yaml" in lines[0] and "already exists" in lines[0], lines[0]


def test_control_no_orphan_warning_when_the_tenant_exists(tmp_path: Path, capsys) -> None:
    tree = next(t for t in MATRIX["trees"]
                if t["name"] == "c2-c5-platform-only-keys-tenant-file-TX.yaml")
    _build(tree, tmp_path)
    load_tenant_tree(str(tmp_path))
    assert "already exists" not in capsys.readouterr().err


def test_each_tenant_is_listed_once(tmp_path: Path) -> None:
    """`all_tenants` used to list a tenant once per file that named it."""
    from _grar_parse import _parse_config_files
    tree = next(t for t in MATRIX["trees"]
                if t["name"] == "c3-same-key-both-files-tenant-file-tx.yaml")
    _build(tree, tmp_path)
    assert _parse_config_files(str(tmp_path))["all_tenants"] == ["tx"]


def test_existence_is_not_consulted_when_no_platform_file_names_a_tenant() -> None:
    """The recursive read behind `exists` is a thunk: a tree whose platform
    files carry no `tenants:` block never pays for it."""
    def boom() -> set:
        raise AssertionError("existence read although no platform entry exists")
    merged, orphans = overlay_platform_tenants(
        [("tx.yaml", "tx", {"a": 1})], boom)
    assert merged == {"tx": {"a": 1}} and orphans == []
