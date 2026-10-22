"""A threshold value's ``value[:severity]`` suffix ↔ the exporter (#3071):
Python half of tests/shared/threshold_value_matrix.json.

The matrix's verdict columns and ``severities`` are written by Go
(components/threshold-exporter/app/pkg/config/threshold_value_parity_test.go,
from ``splitThresholdSeverity`` and ``ThresholdSeverities``, the one grammar
the threshold rows and custom alerts share). Held to the same columns here:

* ``_lib_validation.split_threshold_severity`` — the shared Python reader;
* ``backtest_threshold.parse_threshold_value`` — how the backtest reads a
  value written for a base key (a value the exporter cannot read raises);
* ``check_threshold_unit_sanity._as_number`` — the number the unit-sanity
  lint judges (None for a value the exporter cannot read);
* ``custom_alerts/shape.py`` ``parse_threshold`` — the custom-alert compiler;
* ``_lib_validation.custom_alert_disabled`` and the tool that reads a
  custom alert's opt-out through it (``config_diff._threshold_disabled``)
  — against the ``custom_alert_disabled`` column, which Go writes from
  ``resolveOneCustomAlert`` itself;
* the severity sets the backtest and the compiler compare against;
* tenant-config.schema.json ``thresholdScalar``'s ``pattern``, which checks
  the suffix only (``ok`` is false only for a refused suffix), through ``re``
  and jsonschema.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools", "dx"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools", "lint"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools"))

import _lib_validation  # noqa: E402
import backtest_threshold as bt  # noqa: E402
import config_diff  # noqa: E402
import check_threshold_unit_sanity as unit_sanity  # noqa: E402
from custom_alerts import shape  # noqa: E402

_MATRIX = os.path.join(_REPO_ROOT, "tests", "shared", "threshold_value_matrix.json")
_SCHEMA = os.path.join(_REPO_ROOT, "docs", "schemas", "tenant-config.schema.json")

# The samples #3071 is about, at the least.
_REQUIRED_SAMPLES = {"70", "70:critical", "70:Critical", "70:high", "70:", "7O:critical"}


def _matrix() -> dict:
    with open(_MATRIX, encoding="utf-8") as f:
        return json.load(f)


def _rows() -> list[dict]:
    return _matrix()["rows"]


def _pattern() -> str:
    with open(_SCHEMA, encoding="utf-8") as f:
        return json.load(f)["definitions"]["thresholdScalar"]["pattern"]


def test_matrix_is_written_by_go():
    m = _matrix()
    assert m["severities"], "no severities: regenerate with DA_REGEN_THRESHOLD_VALUE_MATRIX=1"
    assert all({"value", "ok"} <= set(r) for r in m["rows"]), (
        "a row without Go's verdict: regenerate with DA_REGEN_THRESHOLD_VALUE_MATRIX=1")
    assert _REQUIRED_SAMPLES <= {r["value"] for r in m["rows"]}
    assert {r["ok"] for r in m["rows"]} == {True, False}, "the table needs both verdicts"


def test_severity_sets_are_gos():
    assert tuple(_matrix()["severities"]) == bt._COMPARED_SEVERITIES
    assert tuple(_matrix()["severities"]) == shape.THRESHOLD_SEVERITIES
    assert tuple(_matrix()["severities"]) == _lib_validation.THRESHOLD_SEVERITIES


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_shared_reader_splits_as_the_exporter_does(row):
    if not row["ok"]:
        with pytest.raises(ValueError):
            _lib_validation.split_threshold_severity(row["value"])
        return
    assert _lib_validation.split_threshold_severity(row["value"]) == (
        row["value_part"], row["severity"]), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_unit_sanity_lint_judges_the_number_the_exporter_serves(row):
    got = unit_sanity._as_number(row["value"])
    if not (row["ok"] and row["float_ok"]):
        assert got is None, row
        return
    want = float(row["value_part"])
    assert got == want or (math.isnan(got) and math.isnan(want)), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_backtest_reads_a_base_key_value_as_the_exporter_does(row):
    value = row["value"]
    if bt.is_disabled(value):
        assert bt.parse_threshold_value("mysql_connections", value) is None
        return
    if not (row["ok"] and row["float_ok"]):
        with pytest.raises(ValueError):
            bt.parse_threshold_value("mysql_connections", value)
        return
    got = bt.parse_threshold_value("mysql_connections", value)
    want = float(row["value_part"])
    assert got[1] == row["severity"], row
    assert got[0] == want or (math.isnan(got[0]) and math.isnan(want)), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_custom_alert_compiler_splits_as_the_exporter_does(row):
    if not row["ok"]:
        with pytest.raises(shape.RecipeError):
            shape.parse_threshold(row["value"])
        return
    assert shape.parse_threshold(row["value"]) == (row["value_part"], row["severity"]), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_pattern_refuses_exactly_the_refused_suffixes(row):
    assert (re.search(_pattern(), row["value"]) is not None) == row["ok"], row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_validator_refuses_exactly_the_refused_suffixes(row):
    jsonschema = pytest.importorskip("jsonschema")
    with open(_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    wrapper = {"$ref": "#/definitions/thresholdScalar", "definitions": schema["definitions"]}
    assert jsonschema.Draft7Validator(wrapper).is_valid(row["value"]) == row["ok"], row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_custom_alert_opt_out_is_read_as_the_exporter_reads_it(row):
    want = row["custom_alert_disabled"]
    assert _lib_validation.custom_alert_disabled(row["value"]) is want, row
    assert config_diff._threshold_disabled(row["value"]) is want, row


def test_matrix_carries_the_opt_out_samples():
    rows = {r["value"]: r for r in _rows()}
    for v in ("disable", "disable:warning", "disable:critical", "disable:bogus",
              "disable:", "DISABLE:CRITICAL", "off:warning", "false:critical"):
        assert "custom_alert_disabled" in rows[v], v
    assert {rows[v]["custom_alert_disabled"] for v in ("disable:critical", "disable:bogus")} == {True, False}
