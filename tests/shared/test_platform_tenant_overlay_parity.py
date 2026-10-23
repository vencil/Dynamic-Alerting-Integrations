"""Python half of tests/shared/platform_tenant_overlay_matrix.json (#1982).

The routing plane (`_grar_parse.load_tenant_tree`, behind generate-routes /
explain-route / `--validate`) must read a root platform file's `tenants:`
block the way the exporter's /metrics plane does: platform value first, the
tenant's own file wins key by key whatever either file is called, a key the
tenant file does not write keeps the platform value, and a platform file
cannot create a tenant. The Go half
(components/threshold-exporter/app/config_platform_tenant_overlay_test.go)
asserts the same table on /metrics.

The `walker` column (#2019) is the walker plane: describe_tenant.py's
`--show-sources` view here, `pkg/config.ResolveEffective` (/effective) on the
Go side — the same effective config and the same `platform_overlay` and
`profile_overlay` (#2117) fields. Since #2906 describe_tenant.py reads
`da-guard effective`, so the rows where its own merge used to differ from
/effective (#2115) hold it to the same column.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
from _grar_parse import load_tenant_tree  # noqa: E402
from describe_tenant import ConfDScanner  # noqa: E402
from _lib_confd import overlay_platform_tenants  # noqa: E402
import _lib_parse_cache  # noqa: E402
sys.path.insert(0, str(REPO_ROOT / "tests"))
from _go_refusal import go_refused, refuses  # noqa: E402  (#3206)

MATRIX = json.loads((Path(__file__).parent / "platform_tenant_overlay_matrix.json")
                    .read_text(encoding="utf-8"))

# Key sets are exact (the #1967 lesson in the sibling matrix): a misspelt key
# read as absent turns a row into one that tests nothing while staying green.
TOP_KEYS = {"_comment", "trees"}
TREE_KEYS = {"name", "files", "expect"}
# #2368: the alias rows name the threshold their Go `metric` / `tenant_api`
# columns read; optional, and only ever a key this half does not assert.
OPTIONAL_TREE_KEYS = {"metric_key"}
# `metric` / `tenant_api` / `exporter_dedup` / `silent_mode` are the Go
# half's columns (`tenant_api` is the tenant-api merge core, #2208); this half
# asserts `dedup` / `group_wait` / `walker` and only pins the key set of the
# rest.
EXPECT_KEYS = {"metric", "tenant_api", "dedup", "group_wait", "exporter_dedup", "silent_mode", "walker"}
# #2370: `metadata` is the Go half's column too (the exporter's resolved
# `_metadata`, non-empty string fields); optional, only on the metadata rows.
OPTIONAL_EXPECT_KEYS = {"metadata"}
WALKER_KEYS = {"effective_config", "platform_overlay", "profile_overlay"}


def test_matrix_is_not_vacuous() -> None:
    assert MATRIX["trees"] and all(t["expect"] for t in MATRIX["trees"])
    # Every cell of the owner's measurement is present, each file-name
    # spelling included — dropping one would silently narrow the pin.
    names = {t["name"] for t in MATRIX["trees"]}
    for fname in ("tx.yaml", "TX.yaml", "0tx.yaml"):
        assert f"c2-c5-platform-only-keys-tenant-file-{fname}" in names
        assert f"c3-same-key-both-files-tenant-file-{fname}" in names
    # #2368: both cross-spelling directions and their same-spelling control.
    for row in ("a1-alias-tenant-legacy-beats-platform-canonical",
                "a2-alias-tenant-canonical-beats-platform-legacy",
                "a3-alias-control-same-spelling-both-files"):
        assert row in names, row
    # #2414: subtree `_defaults.yaml` levels across the alias spellings.
    for row in ("s1-alias-subtree-canonical-does-not-displace-tenant-legacy",
                "s2-alias-control-subtree-legacy-beats-root-canonical",
                "s3-alias-deeper-subtree-legacy-beats-shallower-canonical"):
        assert row in names, row


def test_matrix_keys_are_exactly_the_known_ones() -> None:
    assert set(MATRIX) == TOP_KEYS, set(MATRIX) ^ TOP_KEYS
    for tree in MATRIX["trees"]:
        assert TREE_KEYS <= set(tree) <= TREE_KEYS | OPTIONAL_TREE_KEYS, (
            tree.get("name"), set(tree) ^ TREE_KEYS)
        for tenant, want in tree["expect"].items():
            assert EXPECT_KEYS <= set(want) <= EXPECT_KEYS | OPTIONAL_EXPECT_KEYS, (
                tree["name"], tenant, set(want) ^ EXPECT_KEYS)
            md = want.get("metadata")
            assert md is None or (isinstance(md, dict) and md
                                  and all(isinstance(v, str) and v for v in md.values())), (
                tree["name"], tenant, md)
            walker = want["walker"]
            if walker is not None:
                assert set(walker) == WALKER_KEYS, (
                    tree["name"], tenant, set(walker) ^ WALKER_KEYS)
                assert isinstance(walker["effective_config"], dict), (tree["name"], tenant)
                overlay = walker["platform_overlay"]
                # null = omitted; an empty list would be a shape neither side emits.
                assert overlay is None or (
                    overlay and all(set(e) == {"file", "keys"} and e["keys"] for e in overlay)
                ), (tree["name"], tenant, overlay)
                profile = walker["profile_overlay"]
                assert profile is None or (
                    profile and all(set(e) == {"profile", "file", "keys"} and e["keys"]
                                    for e in profile)
                ), (tree["name"], tenant, profile)


def _build(tree: dict, root: Path) -> None:
    for rel, content in tree["files"].items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


# #3206: the rows whose tree holds a file the exporter cannot read. Go's half
# still asserts their /metrics column (the exporter serves the rest); a conf.d
# tool refuses the tree instead, naming the file. Pinned here AND checked
# against Go's own answer, so a row cannot drift into or out of this set.
_REFUSED_ROWS = {
    "a-multi-document-tenant-file-declares-nothing": ["sub/tx.yaml"],
    "no-existence-from-a-tenant-file-whose-first-document-does-not-parse": ["sub/tx.yaml"],
}


def _refused(tree: dict, root: Path) -> "list[str]":
    """The paths a conf.d tool must refuse *root* naming ([] for a tree it
    reads), asserted equal to Go's answer."""
    got = go_refused(root)
    assert got == _REFUSED_ROWS.get(tree["name"], []), (tree["name"], got)
    return got


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_routing_plane_matches_the_table(tree, tmp_path: Path, capsys) -> None:
    _build(tree, tmp_path)
    refused = _refused(tree, tmp_path)
    if refused:
        with refuses(*refused):
            load_tenant_tree(str(tmp_path))
        return
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


@pytest.mark.parametrize("tree", MATRIX["trees"], ids=lambda t: t["name"])
def test_walker_plane_matches_the_table(tree, tmp_path: Path) -> None:
    """describe_tenant (the walker-plane reference) serves the `walker` column:
    the root platform files' per-tenant values in the effective config, the
    `platform_overlay` attribution (omitted when empty), and no tenant a
    platform file alone names."""
    _build(tree, tmp_path)
    refused = _refused(tree, tmp_path)
    if refused:
        with refuses(*refused):
            ConfDScanner(tmp_path)
        return
    scanner = ConfDScanner(tmp_path)
    for tenant, want in tree["expect"].items():
        if want["walker"] is None:
            assert tenant not in scanner.tenants, (tree["name"], tenant)
            continue
        info = scanner.source_info(tenant)
        want_cfg = want["walker"]["effective_config"]
        assert info["effective_config"] == want_cfg, (
            tree["name"], tenant, info["effective_config"])
        assert info.get("platform_overlay") == want["walker"]["platform_overlay"], (
            tree["name"], tenant, info.get("platform_overlay"))
        # Omitted, not null/empty, when the layer supplies nothing — the Go
        # field is `platform_overlay,omitempty`.
        assert ("platform_overlay" in info) == (want["walker"]["platform_overlay"] is not None)
        # #2117: the profile's attribution, same omission rule
        # (`profile_overlay,omitempty`).
        assert info.get("profile_overlay") == want["walker"]["profile_overlay"], (
            tree["name"], tenant, info.get("profile_overlay"))
        assert ("profile_overlay" in info) == (want["walker"]["profile_overlay"] is not None)
    assert set(scanner.tenants) <= set(tree["expect"]), (tree["name"], set(scanner.tenants))


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
        [("tx.yaml", "tx", {"a": 1})], boom, platform=())
    assert merged == {"tx": {"a": 1}} and orphans == []


def test_a_file_that_cannot_be_read_or_parsed_declares_nothing(tmp_path: Path) -> None:
    """The existence set (`_lib_parse_cache.declared_tenants`, Go's
    `TenantScope` — #3206) takes a tenant file it cannot use as declaring
    nothing AND as leaving the set open (its `unknown` half), rather than
    raising: a first document that does not parse, and bytes that are not
    UTF-8 (the read itself fails). Neither declares its tenant — the Go
    walker rejects both too (the parse-failure row of the matrix pins the
    first case against Go). A readable control in the same tree still counts.

    ⚠️ The unreadable case is non-UTF-8 content, not `chmod 000`: this suite
    runs as root in CI containers, where mode 000 does not stop a read, and a
    dangling symlink or a directory named `*.yaml` never reaches this code —
    `iter_config_files` drops both before the read.

    #3206: that skip is what a judge (which names such files itself) reads;
    any other tool reaching it is refused by the shared exit, both files named.
    """
    (tmp_path / "broken.yaml").write_text("tenants:\n  tb: [\n", encoding="utf-8")
    (tmp_path / "latin1.yaml").write_bytes(b"tenants:\n  tl: {}\n# caf\xe9\n")
    (tmp_path / "ok.yaml").write_text("tenants:\n  tok: {}\n", encoding="utf-8")
    with _lib_parse_cache.judging():
        declared, unknown = _lib_parse_cache.declared_tenants(
            _lib_parse_cache.reader_for(tmp_path))
        assert set(declared) == {"tok"}
        assert sorted(u.file for u in unknown) == ["broken.yaml", "latin1.yaml"]
    with refuses("broken.yaml", "latin1.yaml"):
        _lib_parse_cache.declared_tenants(_lib_parse_cache.reader_for(tmp_path))
