"""A threshold value's ``value[:severity]`` suffix ↔ the exporter (#3071):
Python half of tests/shared/threshold_value_matrix.json.

The matrix's verdict columns and ``severities`` are written by Go
(components/threshold-exporter/app/pkg/config/threshold_value_parity_test.go,
from ``splitThresholdSeverity`` and ``ThresholdSeverities``, the one grammar
the threshold rows and custom alerts share). Held to the same columns here:

* ``_lib_validation.split_threshold_severity`` — the shared Python reader;
* ``_lib_validation.parse_threshold_number`` — the number half, finite only
  (#3140), against ``float_ok`` (Go's parseThresholdNumber);
* ``backtest_threshold._served_written`` — how ``backtest --tenant`` reads a
  value written for a base key: from da-guard served-values (#3140 PR2), so
  this checks the Python reading of served-values' answer against Go's
  ``row_disabled`` / ``ok`` / ``float_ok`` columns, not a Python mirror
  (``_lib_validation.is_disabled`` and ``parse_threshold_value``, the
  mirrors it replaced, are gone);
* ``check_threshold_unit_sanity._as_number`` — the number the unit-sanity
  lint judges (None for a value the exporter cannot read);
* the severity set the backtest compares against;
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
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools", "lint"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "tools"))

import _lib_validation  # noqa: E402
import backtest_threshold as bt  # noqa: E402
import check_threshold_unit_sanity as unit_sanity  # noqa: E402

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
    assert math.isfinite(got) and got == _number(row["value_part"]), row


def _number(text):
    """The number of a `value_part` Go's `float_ok` accepts: a hex float by
    `float.fromhex`, anything else by `float` (both without the `_` Go and
    Python allow between digits)."""
    text = text.replace("_", "")
    return float.fromhex(text) if "x" in text.lower() else float(text)


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_unit_sanity_lint_judges_the_number_the_exporter_serves(row):
    """The number the exporter serves — and, owner ruling on #3140 (D), a
    non-finite one too, which the exporter refuses but this lint judges
    (±Inf is OUT-OF-DOMAIN)."""
    got = unit_sanity._as_number(row["value"])
    if row["ok"] and "x" in row["value_part"].lower():
        # A hex float, which the exporter reads: this lint never judged one
        # and does not start to without an owner ruling (it would turn
        # `--ci` from 0 to 1; see `_as_number`).
        assert got is None, row
        return
    if row["ok"] and not row["float_ok"] and _non_finite_text(row["value_part"]):
        want = float(row["value_part"])
        assert got is not None and (got == want or (math.isnan(got) and math.isnan(want))), row
        return
    if not (row["ok"] and row["float_ok"]):
        assert got is None, row
        return
    assert got == _number(row["value_part"]), row


def _non_finite_text(text):
    """Whether Python reads `text` as NaN or ±inf at all (`1e999` included)."""
    try:
        return not math.isfinite(float(text))
    except ValueError:
        return False


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_backtest_reads_a_base_key_value_as_the_exporter_does(row):
    """Judged by Go's columns alone (#3141): `row_disabled` is no row,
    `ok` and `float_ok` a row of that number and severity, and anything else
    a value the exporter refuses — which `backtest --tenant` either does not
    backtest (`_NotRead`) or backtests at the platform default the exporter
    serves instead (80, warning; #3140). Read through served-values (#3140
    PR2): a reading of Go's answer, held to Go's own columns."""
    value = row["value"]
    try:
        (got,) = bt._served_written("mysql_connections", (value,))
    except bt._NotRead:
        got = "not read"
    if row["row_disabled"]:
        assert got is None, row
    elif row["ok"] and row["float_ok"] and bt._SPELLINGS_NOT_BACKTESTED.search(value):
        # `1_000`, `0x1p4`: read by the exporter, not backtested by
        # `--tenant` (as before #3140 PR2; `_SPELLINGS_NOT_BACKTESTED`).
        assert got == "not read", row
    elif row["ok"] and row["float_ok"]:
        assert got == (float(row["value_part"]), row["severity"]), row
    else:
        assert got in ("not read", (80.0, "warning")), (row, got)


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


def test_matrix_carries_the_go_number_spellings():
    """#3140: what Go's ParseFloat reads beyond Python's `float` (a hex
    float) and the `_` both read between digits — so the Python readers
    are held to Go on them, not to a conservative refusal of their own."""
    rows = {r["value"]: r for r in _rows()}
    for v in ("1_000", "-1_0", "1e1_0", "0x1p4", "0x_1p4", "0x1.p1"):
        assert rows[v]["ok"] and rows[v]["float_ok"] is True, v
    for v in ("1__0", "_1", "1_", "0x10", "0x1p_4", "0x1p99999", "\u0661\u0660"):
        assert rows[v]["ok"] and rows[v]["float_ok"] is False, v


def test_matrix_carries_the_opt_out_samples():
    rows = {r["value"]: r for r in _rows()}
    for v in ("disable", "disable:warning", "disable:critical", "disable:bogus",
              "disable:", "DISABLE:CRITICAL", "off:warning", "false:critical"):
        assert "custom_alert_disabled" in rows[v], v
    assert {rows[v]["custom_alert_disabled"] for v in ("disable:critical", "disable:bogus")} == {True, False}
