"""Tests for sync_schema.py — the JSON-Schema ↔ Go reserved-key drift tool.

This file IS the CI gate for schema↔Go drift: `test_no_drift_between_schema_and_go`
runs in the normal pytest suite, so a future schema-only key (the `_operator`
class of bug) or a Go key absent from the schema fails CI — without relying on
the manual-stage `schema-check` pre-commit hook ever being run.

`extract_go_keys` previously read `app/config.go` (where the map does NOT live),
so it returned empty — and its only caller, the manual-stage `schema-check`
hook, was never run anyway; these tests pin the corrected `pkg/config/types.go`
path and put the Go↔schema gate in normal CI.
"""
import os

import pytest

import sync_schema as ss

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GO_SRC = os.path.join(_REPO, "components", "threshold-exporter", "app")
_SCHEMA = os.path.join(_REPO, "docs", "schemas", "tenant-config.schema.json")

# Gate on the Go source TREE, not a specific file path: skipping on
# pkg/config/types.go's existence would make the whole module silently skip if
# that file is ever moved/renamed — exactly the case extract_go_keys' glob
# fallback handles, so the gate must still run. Only a Python-only checkout (no
# Go tree at all) legitimately skips.
pytestmark = pytest.mark.skipif(
    not os.path.isdir(_GO_SRC),
    reason="Go source tree not present (Python-only checkout)")


class TestExtractGoKeys:
    """extract_go_keys must read the REAL Go map, not return empty."""

    def test_reads_real_reserved_keys(self):
        keys, prefixes = ss.extract_go_keys(_GO_SRC)
        # The bug: reading app/config.go returned an empty set. Non-empty proves
        # the corrected pkg/config/types.go path (or the glob fallback) works.
        assert keys, "extract_go_keys returned no keys — wrong Go file?"
        # _custom_alerts is the v2.9.0 key whose drift motivated this fix.
        assert "_custom_alerts" in keys
        assert {"_silent_mode", "_severity_dedup", "_metadata"} <= keys
        assert set(prefixes) == {"_state_", "_routing"}


class TestSchemaInSync:
    """The committed schema must match the Go reserved keys — the gate that
    would have caught the schema-only `_operator` drift."""

    def test_no_drift_between_schema_and_go(self):
        keys, prefixes = ss.extract_go_keys(_GO_SRC)
        schema_keys = ss.extract_schema_keys(_SCHEMA)
        missing, extra = ss.check_drift(keys, prefixes, schema_keys)
        assert not missing, f"schema is missing Go reserved keys: {sorted(missing)}"
        assert not extra, (
            f"schema declares properties absent from Go (schema-only drift, e.g. "
            f"the removed `_operator`): {sorted(extra)}")
