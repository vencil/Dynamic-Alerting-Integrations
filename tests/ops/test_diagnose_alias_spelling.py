"""#2368: diagnose's inheritance chain layers threshold keys per THRESHOLD,
not per spelling — the #1231 alias rows of the shared overlay matrix
(tests/shared/platform_tenant_overlay_matrix.json, the rows naming
`metric_key`) must resolve to the value /metrics serves (their `metric`
column, which the Go half asserts against the exporter).

Measured before the fix on row a1 (tenant legacy 90, platform canonical 70):
`resolved` carried the canonical key at the platform's 70 beside the
tenant's legacy 90.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
import diagnose  # noqa: E402
from _grar_validate import DEPRECATED_KEY_ALIASES, _legacy_tenant_key  # noqa: E402
from _lib_confd import overlay_platform_tenants  # noqa: E402

MATRIX = json.loads((REPO_ROOT / "tests" / "shared" / "platform_tenant_overlay_matrix.json")
                    .read_text(encoding="utf-8"))
ALIAS_ROWS = [t for t in MATRIX["trees"] if t.get("metric_key")]


def test_alias_rows_exist() -> None:
    assert len(ALIAS_ROWS) >= 3, [t["name"] for t in ALIAS_ROWS]


@pytest.mark.parametrize("tree", ALIAS_ROWS, ids=lambda t: t["name"])
def test_resolved_carries_the_served_value_once(tree: dict, tmp_path: Path) -> None:
    for rel, content in tree["files"].items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    key = tree["metric_key"]
    spellings = {key, _legacy_tenant_key(key)}
    chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
    got = {k: v for k, v in chain["resolved"].items() if k in spellings}
    assert len(got) == 1, (tree["name"], got)
    assert float(next(iter(got.values()))) == tree["expect"]["tx"]["metric"], (tree["name"], got)


def test_profile_layer_excludes_a_threshold_the_tenant_writes_in_the_other_spelling(
        tmp_path: Path) -> None:
    """The profile only fills keys the tenant does not set under ANY spelling
    (Go profileFill / hasAliasEquivalent): with the tenant on the legacy
    spelling, the profile's canonical value reaches neither the chain's
    profile layer nor `resolved` (#2368 round 2, F-4)."""
    legacy, canon = next(iter(DEPRECATED_KEY_ALIASES.items()))
    (tmp_path / "_defaults.yaml").write_text(f"defaults:\n  {canon}: 30\n", encoding="utf-8")
    (tmp_path / "_profiles.yaml").write_text(
        f"profiles:\n  std:\n    {canon}: 50\n    pg_connections: 7\n", encoding="utf-8")
    (tmp_path / "tx.yaml").write_text(
        f"tenants:\n  tx:\n    _profile: std\n    {legacy}: '90'\n", encoding="utf-8")
    chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
    profile = [layer for layer in chain["chain"] if layer["layer"] == "profile"]
    assert len(profile) == 1, chain["chain"]
    assert canon not in profile[0]["keys"], profile[0]
    assert profile[0]["keys"] == {"pg_connections": 7}, profile[0]  # the fill-in still works
    assert {k: v for k, v in chain["resolved"].items() if k in (canon, legacy)} == {legacy: "90"}


def test_routing_readers_keep_literal_key_merge() -> None:
    """Without `merge=`, overlay_platform_tenants is the per-literal-key
    `dict.update` the routing readers (_grar_parse, check_routing_profiles)
    have always had — #2368 changes only the callers that pass one."""
    legacy, canon = next(iter(DEPRECATED_KEY_ALIASES.items()))
    entries = [("_p.yaml", "tx", {canon: 70, "_routing": {"group_wait": "10s"}}),
               ("tx.yaml", "tx", {legacy: 90})]
    merged, _ = overlay_platform_tenants(entries, lambda: {"tx"})
    assert merged["tx"] == {canon: 70, "_routing": {"group_wait": "10s"}, legacy: 90}
