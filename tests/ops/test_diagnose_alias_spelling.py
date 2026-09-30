"""#2368: diagnose's inheritance chain layers threshold keys per THRESHOLD,
not per spelling — the #1231 alias rows of the shared overlay matrix
(tests/shared/platform_tenant_overlay_matrix.json, the top-level rows naming
`metric_key` — see ALIAS_ROWS for why subtree rows are excluded) must resolve to the value /metrics serves (their `metric`
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
# ⛔ Top-level trees only. diagnose's reader is FLAT by design (#1911,
# `warn_nested`): a tree with files below the root — the #2414 subtree rows —
# is read as its top level alone, with a WARN naming the skipped files, even
# when every layer uses the same spelling (the tenant file itself is skipped).
# "One effective value across spellings" for those rows is held only by the
# Go half of the matrix (/metrics, plus assertWalkerAgreesWithMetrics); the
# Python half checks describe_tenant's walker column, which keeps each
# spelling separately. A recursive diagnose is its own change.
ALIAS_ROWS = [t for t in MATRIX["trees"]
              if t.get("metric_key") and not any("/" in rel for rel in t["files"])]


def test_alias_rows_exist() -> None:
    assert len(ALIAS_ROWS) >= 3, [t["name"] for t in ALIAS_ROWS]


@pytest.mark.parametrize("tree", ALIAS_ROWS, ids=lambda t: t["name"])
def test_resolved_carries_the_served_value_once(tree: dict, tmp_path: Path) -> None:
    for rel, content in tree["files"].items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
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


def _cr_trees() -> "list[tuple[str, dict, dict]]":
    """#2420 (CodeRabbit) shapes: (name, files, resolved threshold keys
    expected). Each expected value is what Go's /metrics serves for the tree
    (measured with LoadDir + Resolve on the PR head): a layer writing BOTH
    spellings serves only its canonical one, and a tenant-layer null falls
    back to the defaults."""
    legacy, canon = next(iter(DEPRECATED_KEY_ALIASES.items()))
    return [
        ("tenant-null-legacy-over-chain-canonical",
         {"_defaults.yaml": f"defaults:\n  {canon}: 70\n",
          "tx.yaml": f"tenants:\n  tx:\n    {legacy}: null\n"},
         {canon: 70}),
        # The null still displaces the platform entry's other spelling (as
        # on /metrics), so the defaults' 30 is served, not the platform's 70.
        ("tenant-null-legacy-over-platform-canonical",
         {"_defaults.yaml": f"defaults:\n  {canon}: 30\ntenants:\n  tx:\n    {canon}: 70\n",
          "tx.yaml": f"tenants:\n  tx:\n    {legacy}: null\n"},
         {canon: 30}),
        ("chain-writes-both-spellings",
         {"_defaults.yaml": f"defaults:\n  {legacy}: 40\n  {canon}: 30\n",
          "tx.yaml": "tenants:\n  tx:\n    redis_x: '1'\n"},
         {canon: 30}),
        ("tenant-writes-both-spellings",
         {"_defaults.yaml": f"defaults:\n  {canon}: 30\n",
          "tx.yaml": f"tenants:\n  tx:\n    {legacy}: '40'\n    {canon}: '50'\n"},
         {canon: "50"}),
        ("platform-entry-writes-both-spellings",
         {"_defaults.yaml": f"defaults:\n  {canon}: 30\ntenants:\n  tx:\n    {legacy}: 40\n    {canon}: 50\n",
          "tx.yaml": "tenants:\n  tx:\n    redis_x: '1'\n"},
         {canon: 50}),
        # #2420 round 4: canonical null + legacy value in ONE tenant layer —
        # the canonical null wins inside the layer, so /metrics falls back to
        # the defaults (Go measured: 10), not the legacy 51.
        ("tenant-writes-canonical-null-and-legacy-value",
         {"_defaults.yaml": f"defaults:\n  mysql_connections: 80\n  {canon}: 10\n",
          "tx.yaml": f"tenants:\n  tx:\n    redis_x: '1'\n    {canon}: null\n    {legacy}: '51'\n"},
         {canon: 10, "mysql_connections": 80}),
        # #2420 round 4: the elected profile writes both spellings — its
        # canonical value is filled (Go measured: 20), not the legacy 21.
        ("profile-writes-both-spellings",
         {"_defaults.yaml": f"defaults:\n  mysql_connections: 80\n  {canon}: 10\n",
          "_profiles.yaml": f"profiles:\n  p:\n    {canon}: '20'\n    {legacy}: '21'\n",
          "tx.yaml": "tenants:\n  tx:\n    redis_x: '1'\n    _profile: p\n"},
         {canon: "20", "mysql_connections": 80}),
        # #2418: a canonical null beside a legacy value in the ROOT defaults
        # writes only the legacy one (Go `levelWritesSpelling`), so /metrics
        # serves the 30 (Go measured: 30), not the null's 0. `resolved` keeps
        # the spelling that supplied the value.
        ("root-canonical-null-legacy-value",
         {"_defaults.yaml": f"defaults:\n  mysql_connections: 80\n  {canon}: null\n  {legacy}: 30\n",
          "tx.yaml": "tenants:\n  tx:\n    redis_x: '1'\n"},
         {legacy: 30, "mysql_connections": 80}),
        # Not an alias shape: a tenant null on a plain threshold also falls
        # back to the defaults on /metrics.
        ("tenant-null-plain-threshold",
         {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
          "tx.yaml": "tenants:\n  tx:\n    mysql_connections: null\n"},
         {"mysql_connections": 80}),
    ]


@pytest.mark.parametrize("name,files,want", _cr_trees(), ids=lambda x: x if isinstance(x, str) else "")
def test_resolved_matches_metrics_for_same_layer_pairs_and_nulls(
        name: str, files: dict, want: dict, tmp_path: Path) -> None:
    for rel, content in files.items():
        (tmp_path / rel).write_text(content, encoding="utf-8")
    legacy, canon = next(iter(DEPRECATED_KEY_ALIASES.items()))
    keys = {legacy, canon, "mysql_connections"}
    chain = diagnose.resolve_inheritance_chain("tx", str(tmp_path))
    got = {k: v for k, v in chain["resolved"].items() if k in keys}
    assert got == want, (name, got)


def test_routing_readers_keep_literal_key_merge() -> None:
    """Without `merge=`, overlay_platform_tenants is the per-literal-key
    `dict.update` the routing readers (_grar_parse, check_routing_profiles)
    have always had — #2368 changes only the callers that pass one."""
    legacy, canon = next(iter(DEPRECATED_KEY_ALIASES.items()))
    entries = [("_p.yaml", "tx", {canon: 70, "_routing": {"group_wait": "10s"}}),
               ("tx.yaml", "tx", {legacy: 90})]
    merged, _ = overlay_platform_tenants(entries, lambda: {"tx"})
    assert merged["tx"] == {canon: 70, "_routing": {"group_wait": "10s"}, legacy: 90}
