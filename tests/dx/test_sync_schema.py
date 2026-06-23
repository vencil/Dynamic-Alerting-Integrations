"""Tests for sync_schema.py — the Schema ↔ Go ↔ Python reserved-key drift tool.

This file IS a CI gate for the drift triangle: `test_no_drift_between_schema_and_go`
and `test_no_drift_three_way` run in the normal pytest suite, so a future
schema-only key (the `_operator` class of bug), a Go key absent from the schema,
or a Python allowlist that lags Go all fail CI — without relying on the
manual-stage `schema-check` pre-commit hook ever being run.

`extract_go_keys` previously read `app/config.go` (where the map does NOT live),
so it returned empty — and its only caller, the manual-stage `schema-check`
hook, was never run anyway; these tests pin the corrected `pkg/config/types.go`
path and put the gate in normal CI.

#658 added the explicit Python leg (extract_python_keys / check_py_go_parity):
Go↔Python was already gated by tests/shared/test_reserved_key_py_go_parity.py, so
the triangle closed by transitivity; checking Schema↔Python here removes that
transitive reliance.
"""
import os

import pytest

import sync_schema as ss

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GO_SRC = os.path.join(_REPO, "components", "threshold-exporter", "app")
_SCHEMA = os.path.join(_REPO, "docs", "schemas", "tenant-config.schema.json")
_PY_SRC = os.path.join(_REPO, "scripts", "tools", "_lib_constants.py")

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


class TestExtractPythonKeys:
    """extract_python_keys must read the REAL Python allowlist, and must NOT be
    fooled by a quoted token inside a `#` comment (the _lib_constants.py comment
    on `_custom_alerts` literally contains `component="custom"`)."""

    def test_reads_real_reserved_keys(self):
        keys, prefixes = ss.extract_python_keys(_PY_SRC)
        assert keys, "extract_python_keys returned no keys — wrong file/regex?"
        assert "_custom_alerts" in keys
        assert {"_silent_mode", "_severity_dedup", "_metadata"} <= keys
        assert set(prefixes) == {"_state_", "_routing"}
        # The `# ... component="custom"` comment must NOT leak a bogus `custom`
        # key — without `#`-stripping it would, inflating the set and crying
        # false Python drift.
        assert "custom" not in keys

    def test_comment_quoted_token_not_extracted(self, tmp_path):
        """A deprecate-by-comment (and any quoted token in a comment) is GONE."""
        synthetic = (
            'VALID_RESERVED_KEYS: Final[set[str]] = {\n'
            '    "_silent_mode",\n'
            '    # "_deprecated": true,  emits "ghost" tokens if not stripped\n'
            '}\n'
            'VALID_RESERVED_PREFIXES: Final[tuple[str, ...]] = ("_state_",)\n'
        )
        tmp = tmp_path / "synthetic_constants.py"
        tmp.write_text(synthetic, encoding="utf-8")
        keys, prefixes = ss.extract_python_keys(str(tmp))
        assert keys == {"_silent_mode"}
        assert prefixes == ["_state_"]


class TestPyGoParity:
    """check_py_go_parity must be byte-for-byte (same shape, no prefix expansion),
    and must DETECT drift in either direction (mutation-proven)."""

    def test_real_python_matches_go(self):
        go_keys, go_prefixes = ss.extract_go_keys(_GO_SRC)
        py_keys, py_prefixes = ss.extract_python_keys(_PY_SRC)
        key_drift, prefix_drift = ss.check_py_go_parity(
            go_keys, go_prefixes, py_keys, py_prefixes)
        assert not key_drift, f"Go↔Python reserved-KEY drift: {sorted(key_drift)}"
        assert not prefix_drift, f"Go↔Python reserved-PREFIX drift: {sorted(prefix_drift)}"

    def test_detects_python_only_key(self):
        # A key Python has but Go lacks (or vice versa) must surface — the exact
        # #741 `_custom_alerts` lag this leg exists to catch.
        key_drift, prefix_drift = ss.check_py_go_parity(
            {"_a", "_b"}, ["_state_"], {"_a", "_b", "_c"}, ["_state_"])
        assert key_drift == {"_c"}
        assert not prefix_drift

    def test_detects_prefix_drift(self):
        key_drift, prefix_drift = ss.check_py_go_parity(
            {"_a"}, ["_state_", "_routing"], {"_a"}, ["_state_"])
        assert not key_drift
        assert prefix_drift == {"_routing"}


class TestThreeWayInSync:
    """The committed Schema, Go, and Python surfaces must all agree."""

    def test_no_drift_three_way(self):
        go_keys, go_prefixes = ss.extract_go_keys(_GO_SRC)
        schema_keys = ss.extract_schema_keys(_SCHEMA)
        py_keys, py_prefixes = ss.extract_python_keys(_PY_SRC)

        missing, extra = ss.check_drift(go_keys, go_prefixes, schema_keys)
        key_drift, prefix_drift = ss.check_py_go_parity(
            go_keys, go_prefixes, py_keys, py_prefixes)

        assert not (missing or extra or key_drift or prefix_drift), (
            "3-way reserved-key drift — Schema/Go/Python disagree. "
            f"schema-missing-go={sorted(missing)}, schema-extra={sorted(extra)}, "
            f"go-python-key-drift={sorted(key_drift)}, "
            f"go-python-prefix-drift={sorted(prefix_drift)}")


class TestExtractPythonKeysEdge:
    """Cover extract_python_keys' no-match fallback (constants block absent)."""

    def test_empty_when_constants_absent(self, tmp_path):
        tmp = tmp_path / "no_constants.py"
        tmp.write_text("X = 1\nY = 'foo'\n", encoding="utf-8")
        keys, prefixes = ss.extract_python_keys(str(tmp))
        assert keys == set()
        assert prefixes == []


class TestPrintDriftReport:
    """print_drift_report: has_drift return + every drift section renders."""

    def test_no_drift_returns_false(self, capsys):
        assert ss.print_drift_report(set(), set(), set(), set()) is False
        assert "in sync" in capsys.readouterr().out

    def test_each_drift_branch_returns_true(self, capsys):
        # One member in each of the four drift buckets → all sections print.
        assert ss.print_drift_report({"_a"}, {"_b"}, {"_c"}, {"_d"}) is True
        out = capsys.readouterr().out
        assert "missing in Schema" in out          # missing_in_schema
        assert "not defined in Go source" in out    # extra_in_schema
        assert "Reserved KEYS differ between Go and Python" in out
        assert "Reserved PREFIXES differ between Go and Python" in out
