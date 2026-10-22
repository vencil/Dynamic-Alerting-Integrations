"""A threshold value's ``value[:severity]`` suffix ↔ the exporter (#3071):
Python half of tests/shared/threshold_value_matrix.json.

The matrix's verdict columns and ``severities`` are written by Go
(components/threshold-exporter/app/pkg/config/threshold_value_parity_test.go,
from ``splitThresholdSeverity`` and ``ThresholdSeverities``, the one grammar
the threshold rows and custom alerts share). Held to the same columns here:

* ``_lib_validation.split_threshold_severity`` — the shared Python reader;
* ``_lib_validation.parse_threshold_number`` — the number half, finite only
  (#3140), against ``float_ok`` (Go's parseThresholdNumber);
* ``_lib_validation.is_disabled`` — a threshold row's switch-off, against the
  ``row_disabled`` column Go writes from thresholdRowDisabled (#3141:
  ``disable:<severity>`` is none, and Go's white space, not ``str.strip``'s);
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
    assert all({"value", "ok", "row_disabled"} <= set(r) for r in m["rows"]), (
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


@pytest.mark.parametrize("row", [r for r in _rows() if r["ok"]], ids=lambda r: repr(r["value"]))
def test_shared_number_reader_is_the_exporters(row):
    """#3140: `float_ok` is Go's parseThresholdNumber — finite only."""
    if not row["float_ok"]:
        with pytest.raises(ValueError):
            _lib_validation.parse_threshold_number(row["value_part"])
        return
    got = _lib_validation.parse_threshold_number(row["value_part"])
    assert math.isfinite(got) and got == float(row["value_part"]), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_threshold_row_switch_off_is_read_as_the_exporter_reads_it(row):
    """#3141: `row_disabled` is Go's thresholdRowDisabled."""
    assert _lib_validation.is_disabled(row["value"]) is row["row_disabled"], row
    assert bt.is_disabled(row["value"]) is row["row_disabled"], row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_unit_sanity_lint_judges_the_number_the_exporter_serves(row):
    """The number the exporter serves — and, owner ruling on #3140 (D), a
    non-finite one too, which the exporter refuses but this lint judges
    (±Inf is OUT-OF-DOMAIN)."""
    got = unit_sanity._as_number(row["value"])
    if row["ok"] and not row["float_ok"] and _non_finite_text(row["value_part"]):
        want = float(row["value_part"])
        assert got is not None and (got == want or (math.isnan(got) and math.isnan(want))), row
        return
    if not (row["ok"] and row["float_ok"]):
        assert got is None, row
        return
    assert got == float(row["value_part"]), row


def _non_finite_text(text):
    """Whether Python reads `text` as NaN or ±inf at all (`1e999` included)."""
    try:
        return not math.isfinite(float(text))
    except ValueError:
        return False


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_backtest_reads_a_base_key_value_as_the_exporter_does(row):
    """Judged by Go's columns alone (#3141): `row_disabled` is no row,
    `ok` and `float_ok` a row of that number and severity, and anything else
    a value the exporter refuses."""
    value = row["value"]
    if row["row_disabled"]:
        assert bt.parse_threshold_value("mysql_connections", value) is None
        return
    if not (row["ok"] and row["float_ok"]):
        with pytest.raises(ValueError):
            bt.parse_threshold_value("mysql_connections", value)
        return
    got = bt.parse_threshold_value("mysql_connections", value)
    assert got == (float(row["value_part"]), row["severity"]), row


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


def test_matrix_carries_the_non_finite_and_row_disable_samples():
    """#3140 / #3141: the samples the two rulings are about, with Go's
    verdict on each."""
    rows = {r["value"]: r for r in _rows()}
    for v in ("NaN", "nan", "Inf", "+Inf", "-Inf", "infinity", "-inf:critical",
              "NaN:warning", "Inf:critical", "1e999"):
        assert rows[v]["ok"] and rows[v]["float_ok"] is False, v
    assert rows["1e-400"]["float_ok"] is True
    for v in ("disable:critical", "disable:warning", "off:critical", "disable\x1c", "disable\x1f"):
        assert rows[v]["row_disabled"] is False, v
    for v in ("disable", " disable ", "OFF", "false", "DİSABLE"):
        assert rows[v]["row_disabled"] is True, v
    assert rows["disable:critical"]["custom_alert_disabled"] is True  # #3042: unchanged


def test_matrix_carries_the_opt_out_samples():
    rows = {r["value"]: r for r in _rows()}
    for v in ("disable", "disable:warning", "disable:critical", "disable:bogus",
              "disable:", "DISABLE:CRITICAL", "off:warning", "false:critical"):
        assert "custom_alert_disabled" in rows[v], v
    assert {rows[v]["custom_alert_disabled"] for v in ("disable:critical", "disable:bogus")} == {True, False}
