"""A threshold value's ``value[:severity]`` suffix ↔ the exporter (#3071):
Python half of tests/shared/threshold_value_matrix.json.

The matrix's verdict columns and ``severities`` are written by Go
(components/threshold-exporter/app/pkg/config/threshold_value_parity_test.go,
from ``splitThresholdSeverity`` and ``ThresholdSeverities``, the one grammar
the threshold rows and custom alerts share). Held to the same columns here:

* ``da-guard threshold-values`` (#3267), as ``_lib_parse_tree.threshold_values``
  reads it — the reading the unit-sanity lint judges. Python has no reader
  of a threshold value of its own any more (the ``_lib_validation`` copy of
  the grammar and of parseThresholdNumber is gone), so this checks the
  Python reading of Go's answer against Go's own columns;
* ``check_threshold_unit_sanity._numbers`` — the number the lint judges
  (None for a value the exporter cannot read, the value itself for a
  non-finite one, #3140 ruling D);
* ``backtest_threshold._served_written`` — how ``backtest --tenant`` reads a
  value written for a base key: from da-guard served-values (#3140 PR2);
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

import _lib_parse_tree  # noqa: E402
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


def _number(text):
    """The number of a `value_part` Go's `float_ok` accepts — test-side only,
    to check the value Go answers: a hex float by `float.fromhex`, anything
    else by `float` (both without the `_` Go and Python allow between
    digits)."""
    text = text.replace("_", "")
    return float.fromhex(text) if "x" in text.lower() else float(text)


def _same(a, b):
    return a == b or (a is not None and b is not None and math.isnan(a) and math.isnan(b))


@pytest.fixture(scope="module")
def _readings(da_guard_binary):
    """Every matrix value read once by `da-guard threshold-values`, for a
    base key and for its `_critical` key."""
    items = [(k, r["value"]) for r in _rows()
             for k in ("mysql_connections", "mysql_connections_critical")]
    got = _lib_parse_tree.threshold_values(items, binary=da_guard_binary)
    return {(g.key, g.text): g for g in got}


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_threshold_values_reads_as_the_matrix_says(row, _readings):
    """#3267: the subcommand's reading of every row is Go's columns —
    severity, a finite number exactly when `ok` and `float_ok`, refused
    exactly when the row resolvers do not read it (not a number, not a
    disable). A `_critical` key differs in `refused` alone: it is read
    whole, so any suffix refuses it."""
    base = _readings[("mysql_connections", row["value"])]
    crit = _readings[("mysql_connections_critical", row["value"])]
    read = row["ok"] and row["float_ok"]
    if not row["ok"]:
        assert (base.severity, base.number, base.non_finite) == ("", None, None), row
    else:
        assert base.severity == row["severity"], row
        assert (base.number is not None) == row["float_ok"], row
        if read:
            assert math.isfinite(base.number) and base.number == _number(row["value_part"]), row
    assert (base.refused == "") == (read or row["row_disabled"]), (row, base)
    assert (crit.severity, crit.number, crit.non_finite is None) == (
        base.severity, base.number, base.non_finite is None), (row, crit)
    whole = read and ":" not in row["value"]
    assert (crit.refused == "") == (whole or row["row_disabled"]), (row, crit)


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_unit_sanity_lint_judges_the_number_the_exporter_reads(row, _readings):
    """The number the exporter reads — every spelling it reads, a hex float
    included (#3267) — and, owner ruling on #3140 (D), a non-finite one too,
    which the exporter refuses but this lint judges (±Inf is OUT-OF-DOMAIN).
    A value whose suffix the exporter refuses is not judged."""
    got = unit_sanity._numbers([("mysql_connections", row["value"])])[0]
    if not row["ok"]:
        assert got is None, row
    elif row["float_ok"]:
        assert got == _number(row["value_part"]), row
    else:
        want = _readings[("mysql_connections", row["value"])].non_finite
        if want is None:
            assert got is None, row
        else:
            assert _same(got, want) and not math.isfinite(got), row


#: The matrix rows that read as NaN or ±Inf (an overflow included): each is
#: judged by the lint as that value, never skipped (#3140 ruling D).
_NON_FINITE_ROWS = {"NaN": math.nan, "nan": math.nan, "Inf": math.inf, "+Inf": math.inf,
                    "-Inf": -math.inf, "infinity": math.inf, "-inf:critical": -math.inf,
                    "NaN:warning": math.nan, "Inf:critical": math.inf, "1e999": math.inf}


@pytest.mark.usefixtures("da_guard_env")
def test_unit_sanity_lint_judges_the_non_finite_samples():
    got = unit_sanity._numbers([("mysql_connections", v) for v in _NON_FINITE_ROWS])
    for (v, want), g in zip(_NON_FINITE_ROWS.items(), got):
        assert g is not None and _same(g, want), (v, g)


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
    elif row["ok"] and row["float_ok"]:
        # `1_000`, `0x1p4` included: backtested at the number the exporter
        # serves (#3267), with no spelling table of this tool's own.
        assert got == (_number(row["value_part"]), row["severity"]), row
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
    float) and the `_` both read between digits — so the readers checked
    here (`da-guard threshold-values`, the lint's judging, #3267) are held
    to Go on them."""
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
