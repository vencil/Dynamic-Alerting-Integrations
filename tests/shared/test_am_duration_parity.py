"""Routing timing durations ↔ what Alertmanager reads (#2490): Python half of
tests/shared/am_duration_matrix.json.

The matrix's ``am_*`` columns are prometheus/common ``model.ParseDuration``'s
verdicts, written by Go
(components/threshold-exporter/app/pkg/config/am_duration_parity_test.go,
which also holds the exporter's clampDuration to them). Held to the same
columns here:

* ``_lib_validation.am_duration_seconds`` — the route generator's parser,
  which reads tenant-config.schema.json ``definitions.duration``;
* ``definitions.duration`` itself, through jsonschema (what
  check_confd_schema.py and editors run) and through ``re`` — except where a
  pattern cannot follow ParseDuration: int64 overflow, computed here
  independently of the code under test;
* every routing timing field of the schemas ``$ref``s that definition;
* what ``validate_and_clamp`` writes for a value Alertmanager takes is a
  value Alertmanager takes.
"""
from __future__ import annotations

import json
import os
import re

import pytest

import _lib_validation
from _lib_constants import GUARDRAILS

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MATRIX = os.path.join(_REPO_ROOT, "tests", "shared", "am_duration_matrix.json")
_SCHEMA_DIR = os.path.join(_REPO_ROOT, "docs", "schemas")
_GO_MOD = os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "go.mod")

# The samples #2490 asked the table to carry, at the least.
_REQUIRED_SAMPLES = {"0", "1h30m", "90m", "1.5h", "1d", "1w", "1y", "30m1h",
                     "1h1h", "1ns", "1us", "500ms", "01h", " 1h", "", "-1h"}

_TIMING_FIELDS = ("group_wait", "group_interval", "repeat_interval")
_DURATION_REF = "#/definitions/duration"


def _matrix() -> dict:
    with open(_MATRIX, encoding="utf-8") as f:
        return json.load(f)


def _rows() -> list[dict]:
    return _matrix()["rows"]


def _schema(name: str = "tenant-config.schema.json") -> dict:
    with open(os.path.join(_SCHEMA_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def _overflows(value: str) -> bool:
    """Whether a value made of ``<digits><unit>`` tokens overflows int64
    nanoseconds — ParseDuration's one refusal no pattern can state. Plain
    integer arithmetic, written apart from am_duration_seconds."""
    ns = {"y": 31536000 * 10**9, "w": 604800 * 10**9, "d": 86400 * 10**9,
          "h": 3600 * 10**9, "m": 60 * 10**9, "s": 10**9, "ms": 10**6}
    total = 0
    for digits, unit in re.findall(r"([0-9]+)(ms|y|w|d|h|m|s)", value):
        total += int(digits) * ns[unit]
    return total > 2**63 - 1


@pytest.fixture
def fresh_rule(monkeypatch):
    monkeypatch.setattr(_lib_validation, "_DURATION_RULE", None)


def test_matrix_is_written_by_go_and_names_the_go_mod_version():
    m = _matrix()
    rows = m["rows"]
    assert rows, "a vacuous table passes nothing"
    assert all({"value", "am_valid"} <= set(r) for r in rows), (
        "a row without Go's verdict: regenerate with DA_REGEN_AM_DURATION_MATRIX=1")
    assert all(("am_ns" in r) == r["am_valid"] == ("am_error" not in r) for r in rows)
    values = [r["value"] for r in rows]
    assert len(values) == len(set(values))
    assert _REQUIRED_SAMPLES <= set(values), _REQUIRED_SAMPLES - set(values)
    assert any(r["am_error"] == "duration out of range" for r in rows if not r["am_valid"])
    with open(_GO_MOD, encoding="utf-8") as f:
        pinned = re.search(r"github\.com/prometheus/common (v\S+)", f.read()).group(1)
    assert m["source"] == f"github.com/prometheus/common {pinned} model.ParseDuration"


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_python_parser_agrees_with_go(row, fresh_rule):
    got = _lib_validation.am_duration_seconds(row["value"])
    if row["am_valid"]:
        assert got == row["am_ns"] / 1e9, (row, got)
    else:
        assert got is None, (row, got)


def test_rule_is_read_from_the_schema(fresh_rule):
    pattern, min_len, description = _lib_validation.duration_rule()
    definition = _schema()["definitions"]["duration"]
    assert pattern.pattern == definition["pattern"]
    assert min_len == definition["minLength"] == 1
    assert description == definition["description"]
    assert definition["type"] == "string"


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_pattern_agrees_with_go_except_overflow(row):
    d = _schema()["definitions"]["duration"]
    v = row["value"]
    by_re = (len(v) >= d["minLength"] and re.fullmatch(d["pattern"], v) is not None)
    if by_re == row["am_valid"]:
        return
    # The only rows a pattern may get wrong: it accepts, Go refuses on range.
    assert by_re and not row["am_valid"] and _overflows(v), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_validator_agrees_with_go_except_overflow(row):
    jsonschema = pytest.importorskip("jsonschema")
    schema = _schema()
    wrapper = {"$ref": _DURATION_REF, "definitions": schema["definitions"]}
    ok = jsonschema.Draft7Validator(wrapper).is_valid(row["value"])
    if ok == row["am_valid"]:
        return
    assert ok and not row["am_valid"] and _overflows(row["value"]), row


def _timing_field_schemas() -> list[tuple[str, dict]]:
    """Every (location, field schema) of a routing timing field in the
    schemas that describe Alertmanager routing."""
    defs = _schema()["definitions"]
    out = []
    for name in ("routing", "routingOverride", "routingRoute", "routingEnforced",
                 "routingDefaults"):
        props = defs[name]["properties"]
        for field in _TIMING_FIELDS:
            out.append((f"{name}.{field}", props[field]))
    return out


@pytest.mark.parametrize("where,field", _timing_field_schemas(),
                         ids=lambda x: x if isinstance(x, str) else "")
def test_every_routing_timing_field_refs_the_definition(where, field):
    nullable = where.startswith("routing.")
    if nullable:
        # #1339: explicit null opts out of an inherited value — kept.
        assert field.get("anyOf") == [{"$ref": _DURATION_REF}, {"type": "null"}], where
    else:
        assert field.get("$ref") == _DURATION_REF, where
    for key in ("pattern", "type"):
        assert key not in field, f"{where}: a second copy of the rule ({key})"


def test_no_other_copy_of_a_go_duration_pattern_in_routing_schemas():
    """The pattern this replaced must not survive in a routing field."""
    for name in ("tenant-config.schema.json", "routing-profiles.schema.json",
                 "platform-defaults.schema.json"):
        text = json.dumps(_schema(name))
        assert "group_wait\": {\"type\": \"string\"" not in text, name
    defs = _schema()["definitions"]
    for name in ("routing", "routingOverride", "routingRoute", "routingEnforced",
                 "routingDefaults"):
        assert "(ns|us|" not in json.dumps(defs[name]), name


@pytest.mark.parametrize("row", [r for r in _rows() if r["am_valid"]],
                         ids=lambda r: repr(r["value"]))
@pytest.mark.parametrize("param", sorted(GUARDRAILS))
def test_clamp_writes_what_alertmanager_reads(row, param):
    out, warnings = _lib_validation.validate_and_clamp(param, row["value"], "t")
    assert _lib_validation.am_duration_seconds(out) is not None, (row, out)
    if not warnings:
        assert out == row["value"]   # within bounds: unchanged
