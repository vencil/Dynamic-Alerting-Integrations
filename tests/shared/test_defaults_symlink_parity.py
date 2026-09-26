"""Python half of tests/shared/defaults_symlink_parity_matrix.json (#1674).

describe_tenant must give the merged_hash the exporter's chain gives
(pkg/config defaults_symlink_parity_test.go asserts the same table) when a
defaults carrier is a symlink. The carrier belongs to the directory that
holds the ENTRY, whatever it points at.
Since #2054 the table also pins tenants the exporter does NOT see at all
(`"absent": true`: hidden files, hidden directories, a ConfigMap mount's
`..<timestamp>/` payload), which describe_tenant must report as not found.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
EXIT_CALLER_ERROR = 2  # _lib_exitcodes.EXIT_CALLER_ERROR: describe_tenant's "not found"
MATRIX = json.loads((Path(__file__).parent / "defaults_symlink_parity_matrix.json")
                    .read_text(encoding="utf-8"))


# ⛔ #1967 blind review: a misspelt optional key (`confd` for `conf_d`) was
# read as absent, the tree fell back to the tree root as conf.d, and the row
# stopped testing what its name says while staying green. Key sets are exact.
TOP_KEYS = {"_comment", "trees"}
TREE_REQUIRED = {"name", "files", "symlinks", "expect"}
TREE_OPTIONAL = {"conf_d"}
# #2054: an `expect` row is exactly ONE shape, chosen by its key set. A mixed
# row (`absent` beside a merged_hash) or a partial one is a broken table — it
# would leave one half unchecked while staying green — so it is rejected, not
# read leniently. A later shape (#2049's `error`) is one more entry here.
EXPECT_SHAPES = {
    "resolved": {"chain_len", "effective_config", "merged_hash"},
    "absent": {"absent"},
}


def expect_shape(want: dict) -> str:
    """The shape of one `expect` row; raises ValueError on anything else."""
    for shape, keys in EXPECT_SHAPES.items():
        if set(want) == keys:
            if shape == "absent" and want["absent"] is not True:
                raise ValueError('"absent" must be true when present')
            return shape
    raise ValueError(f"expect row {sorted(want)} matches no shape "
                     f"{ {k: sorted(v) for k, v in EXPECT_SHAPES.items()} }")


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])
    # An absent-only tree passes on an empty walk; each needs a control.
    for tree in MATRIX["trees"]:
        assert any(expect_shape(w) == "resolved" for w in tree["expect"].values()), (
            tree["name"], "no visible control tenant")


@pytest.mark.parametrize("bad", [
    {"absent": False},
    {"absent": True, "merged_hash": "x"},
    {"absent": True, "chain_len": 1},
    {"absent": True, "effective_config": {}},
    {"chain_len": 1, "effective_config": {}},
    {},
], ids=["absent-false", "absent-with-hash", "absent-with-chain",
        "absent-with-effective", "resolved-missing-hash", "empty"])
def test_expect_shape_rejects_mixed_rows(bad: dict) -> None:
    with pytest.raises(ValueError):
        expect_shape(bad)


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        keys = set(tree)
        assert TREE_REQUIRED <= keys <= TREE_REQUIRED | TREE_OPTIONAL, (
            tree.get("name"), keys ^ (TREE_REQUIRED | (keys & TREE_OPTIONAL)))
        for tenant, want in tree["expect"].items():
            try:
                expect_shape(want)
            except ValueError as exc:
                pytest.fail(f"{tree['name']}/{tenant}: {exc}")


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_describe_tenant_matches_the_pinned_go_answer(tree, tmp_path: Path) -> None:
    for rel, content in tree["files"].items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for link, target in tree["symlinks"].items():
        # `target_is_directory` only matters on Windows; a dangling target
        # (created later in this loop) is fine on POSIX.
        try:
            os.symlink(target, tmp_path / link,
                       target_is_directory=(tmp_path / link).parent.joinpath(target).is_dir())
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")
    # #1967: `conf_d` (optional) names the directory handed over as conf.d,
    # so a tree can hold link targets outside it. Absent = the tree root.
    conf_d = tmp_path / tree.get("conf_d", ".")
    if "conf_d" in tree:
        # `conf_d` exists only so a row can hold a target OUTSIDE conf.d; a
        # row declaring it with every target inside no longer tests that.
        real_conf_d = conf_d.resolve()
        assert any(not (tmp_path / link).resolve().is_relative_to(real_conf_d)
                   for link in tree["symlinks"]), (
            f"{tree['name']}: declares conf_d but no symlink target resolves "
            "outside it — the row no longer tests a target outside conf.d")
    for tenant, want in tree["expect"].items():
        r = subprocess.run(
            [sys.executable, str(DESCRIBE), tenant, "--conf-d", str(conf_d),
             "--show-sources", "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        if expect_shape(want) == "absent":
            # The exporter's walker never reads it, so neither may this tool.
            assert r.returncode == EXIT_CALLER_ERROR, (tenant, r.returncode, r.stdout)
            assert f"Tenant '{tenant}' not found" in r.stderr, (tenant, r.stderr)
            continue
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert len(out["defaults_chain"]) == want["chain_len"], (tenant, out["defaults_chain"])
        assert out["effective_config"] == want["effective_config"], tenant
        assert out["merged_hash"] == want["merged_hash"], tenant
