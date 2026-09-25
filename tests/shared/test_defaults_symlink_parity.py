"""Python half of tests/shared/defaults_symlink_parity_matrix.json (#1674).

describe_tenant must give the merged_hash the exporter's chain gives
(pkg/config defaults_symlink_parity_test.go asserts the same table) when a
defaults carrier is a symlink. The carrier belongs to the directory that
holds the ENTRY, whatever it points at.
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
MATRIX = json.loads((Path(__file__).parent / "defaults_symlink_parity_matrix.json")
                    .read_text(encoding="utf-8"))


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_describe_tenant_matches_the_pinned_go_answer(tree, tmp_path: Path) -> None:
    for rel, content in tree["files"].items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    for link, target in tree["symlinks"].items():
        try:
            os.symlink(target, tmp_path / link)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable here: {exc}")
    # #1967: `conf_d` (optional) names the directory handed over as conf.d,
    # so a tree can hold link targets outside it. Absent = the tree root.
    conf_d = tmp_path / tree.get("conf_d", ".")
    for tenant, want in tree["expect"].items():
        r = subprocess.run(
            [sys.executable, str(DESCRIBE), tenant, "--conf-d", str(conf_d),
             "--show-sources", "--format", "json"],
            capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert len(out["defaults_chain"]) == want["chain_len"], (tenant, out["defaults_chain"])
        assert out["effective_config"] == want["effective_config"], tenant
        assert out["merged_hash"] == want["merged_hash"], tenant
