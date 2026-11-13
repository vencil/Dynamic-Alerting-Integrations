"""A threshold value, ``value[:severity]`` ↔ the exporter (#3071, #3457):
the Python and JSON-schema half of tests/shared/threshold_value_matrix.json.

The matrix's verdict columns and its header are written by Go
(components/threshold-exporter/app/pkg/config/threshold_value_parity_test.go,
from ``splitThresholdSeverity``, ``parseThresholdNumber`` and
``ThresholdSeverities``, the one grammar the threshold rows and custom alerts
share). Held to the same columns here:

* ``_lib_validation.threshold_number`` / ``is_threshold_number_text`` — the
  Python reader of a threshold NUMBER, from the pattern generated out of the
  recipe contract (``grammar_ok`` / ``float_ok``);
* the contract's ``patterns.thresholdNumber`` itself, against the grammar
  written out as a count (``_in_grammar``);
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
* tenant-config.schema.json ``thresholdScalar``'s ``pattern`` (generated:
  a number of the grammar or a disable word, then an optional severity),
  through ``re`` and jsonschema.
"""
from __future__ import annotations

import itertools
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
_CONTRACT = os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "pkg", "config",
                         "recipe_contract.json")

# The samples #3071 is about, at the least.
_REQUIRED_SAMPLES = {"70", "70:critical", "70:Critical", "70:high", "70:", "7O:critical"}

#: The most digits a threshold number's mantissa may have (#3457), every
#: digit counted: leading and trailing zeros, both sides of the `.`.
_MAX_MANTISSA_DIGITS = 20
_LONG_MANTISSA = "1" + "0" * 800 + "e-800"  # #3827: mathematically 1


def _contract_pattern() -> str:
    with open(_CONTRACT, encoding="utf-8") as f:
        return json.load(f)["patterns"]["thresholdNumber"]


def _in_grammar(text: str) -> bool:
    """The grammar of a threshold number, by counting: an optional `+` or
    `-`; a mantissa of ASCII digits with at most one `.`, at least one digit
    and at most `_MAX_MANTISSA_DIGITS`; then optionally `e` or `E`, an
    optional sign and at least one ASCII digit (any number of them)."""
    digits = "0123456789"
    if text[:1] in ("+", "-"):
        text = text[1:]
    mantissa, exponent = text, None
    for i, ch in enumerate(text):
        if ch in "eE":
            mantissa, exponent = text[:i], text[i + 1:]
            break
    count = sum(ch in digits for ch in mantissa)
    if (any(ch not in digits + "." for ch in mantissa) or mantissa.count(".") > 1
            or not 1 <= count <= _MAX_MANTISSA_DIGITS):
        return False
    if exponent is None:
        return True
    if exponent[:1] in ("+", "-"):
        exponent = exponent[1:]
    return exponent != "" and all(ch in digits for ch in exponent)


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
    assert all(({"value_part", "severity", "grammar_ok", "float_ok"} <= set(r)) == r["ok"] for r in m["rows"]), (
        "the value-half columns are written exactly for the rows whose suffix reads")
    assert _REQUIRED_SAMPLES <= {r["value"] for r in m["rows"]}
    assert {r["ok"] for r in m["rows"]} == {True, False}, "the table needs both verdicts"


def test_severity_sets_are_gos():
    assert tuple(_matrix()["severities"]) == bt._COMPARED_SEVERITIES
    assert tuple(_matrix()["severities"]) == _lib_validation.THRESHOLD_SEVERITIES


def _number(text):
    """The number of a `value_part` Go's `float_ok` accepts — test-side only,
    to check the value Go answers. Such a text is in the grammar (decimal,
    at most 20 digits), where `float` rounds as Go does."""
    assert _in_grammar(text), text
    return float(text)


def test_contract_pattern_is_the_documented_count():
    """#3457: patterns.thresholdNumber accepts exactly what the count
    accepts — every string of up to five characters over the grammar's
    alphabet and the characters it must refuse, and every mantissa length
    around the bound with the `.` (or a `_`) at every position. Widening the
    pattern (a `_`, a 21st digit, a hex prefix) fails here."""
    pattern = re.compile(_contract_pattern(), re.ASCII)
    samples = {"".join(s) for n in range(6) for s in itertools.product("09.eE+-_x \n", repeat=n)}
    for digits in range(1, _MAX_MANTISSA_DIGITS + 4):
        run = "7" * digits
        for sign, exp in itertools.product(("", "+", "-"), ("", "e5", "E-5", "e+" + "9" * 40, "e", "e_1")):
            samples.add(sign + run + exp)
            for dot in range(digits + 1):
                samples.add(sign + run[:dot] + "." + run[dot:] + exp)
                samples.add(sign + run[:dot] + "_" + run[dot:] + exp)
    samples |= {"0x10", "0x1p4", "0o17", "0b101", "1_000", "Inf", "NaN", "infinity", "\uff11", "\u0661",
                "1\n", _LONG_MANTISSA}
    assert len(samples) > 150_000
    wrong = sorted(s for s in samples if (pattern.fullmatch(s) is not None) != _in_grammar(s))
    assert not wrong, wrong[:20]
    assert sum(map(_in_grammar, samples)) > 1000, "the sample holds too few accepted strings"


def test_contract_pattern_is_portable():
    """One pattern for Go (RE2), ECMAScript and Python: ASCII classes only,
    no lookaround, no backreference, no flag group — nothing one of the
    three engines reads another way."""
    pattern = _contract_pattern()
    assert pattern.isascii() and pattern.startswith("^") and pattern.endswith("$")
    for construct in ("(?=", "(?!", "(?<", "(?P", "\\d", "\\s", "\\w", "\\b", "(?i", "(?m", "(?s"):
        assert construct not in pattern, construct
    assert not re.search(r"\\[0-9]", pattern), "a backreference"
    assert "[0-9]" in pattern
    # ECMAScript's `v` mode (an HTML `pattern=` attribute) refuses a class
    # with a bare `-` in it (`[-+]`): a sign is written `(?:\+|-)`. The
    # portal's Vitest compiles the pattern with `u` and `v`; this keeps the
    # shape out of the source.
    for cls in re.findall(r"\[[^\]]*\]", pattern):
        assert re.fullmatch(r"\[(?:[A-Za-z0-9]|[A-Za-z0-9]-[A-Za-z0-9])+\]", cls), cls


@pytest.mark.parametrize("row", [r for r in _rows() if r["ok"]], ids=lambda r: repr(r["value"]))
def test_python_reads_a_threshold_number_as_the_matrix_says(row):
    """#3457: `_lib_validation`'s reader of a threshold number is the
    exporter's on every value half — in the grammar exactly when
    `grammar_ok`, a number exactly when `float_ok`."""
    part = row["value_part"]
    assert _lib_validation.is_threshold_number_text(part) == row["grammar_ok"] == _in_grammar(part), row
    got = _lib_validation.threshold_number(part)
    assert (got is not None) == row["float_ok"], row
    if got is not None:
        assert math.isfinite(got) and got == float(part), row


@pytest.mark.parametrize("row", [r for r in _rows() if r["ok"]], ids=lambda r: repr(r["value"]))
def test_python_trims_as_the_matrix_says(row):
    """#3457: `threshold_trim` is the exporter's trim (the matrix header's
    `trim_code_points`, generated into _lib_constants), not `str.strip()`:
    split at the last `:` and trimmed, the value half is Go's `value_part`,
    and a value with no `:` is a number exactly when the row is one."""
    value = row["value"]
    whole = _lib_validation.threshold_trim(value)
    head = whole.rpartition(":")[0] if ":" in whole else whole
    assert _lib_validation.threshold_trim(head) == row["value_part"], row
    if ":" not in value:
        assert (_lib_validation.written_threshold_number(value) is not None) == row["float_ok"], row


def test_python_trim_set_is_the_matrix_header():
    import _lib_constants
    assert _lib_constants.THRESHOLD_TRIM_CODE_POINTS == frozenset(_matrix()["trim_code_points"])
    for text, want in (("\x1c70\x1f", "\x1c70\x1f"), ("\ufeff70", "\ufeff70"), ("\u008570　", "70"),
                       (" \t70\r\n", "70"), ("", ""), ("   ", "")):
        assert _lib_validation.threshold_trim(text) == want, ascii(text)
    # str.strip() disagrees on exactly these, which is why it is not used.
    assert "\x1c70".strip() == "70" and "\u008570".strip() == "70"


def test_python_number_reader_takes_no_text_float_alone_would():
    """`float()` reads all of these; the grammar reads none."""
    for text in ("1_000", " 5", "5 ", "5\n", "nan", "inf", "-Infinity", "\uff15", "\u0665", "1e999",
                 "1" * 21, _LONG_MANTISSA):
        float(text)  # no ValueError: this is what must not be relied on
        assert _lib_validation.threshold_number(text) is None, text
    for value in (None, 5, 5.0, True, b"5"):
        assert _lib_validation.threshold_number(value) is None
        assert not _lib_validation.is_threshold_number_text(value)


def test_threshold_number_text_reads_back_as_the_number():
    """The text a tool writes a number with is in the grammar for every
    finite float — `repr` alone is not (21 digits below)."""
    import random
    import struct
    outside = 1.2345678901234567e-4
    assert not _lib_validation.is_threshold_number_text(repr(outside)), repr(outside)
    rng = random.Random(3457)
    values = [0.0, -0.0, 1.0, 70.0, 0.1, 1e21, 1e-7, outside, -outside, 1.7976931348623157e308, 5e-324,
              0.30000000000000004]
    values += [struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0] for _ in range(50_000)]
    for v in values:
        if math.isfinite(v):
            text = _lib_validation.threshold_number_text(v)
            assert _lib_validation.threshold_number(text) == v, (v, text)
    assert _lib_validation.threshold_number_text(30) == "30.0"
    assert _lib_validation.threshold_number_text(0.8) == "0.8"
    assert _lib_validation.threshold_number_text(-math.inf) == "-inf"
    assert _lib_validation.threshold_number_text(math.nan) == "nan"


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
    """The number the exporter reads (#3267) and, owner ruling on #3140 (D),
    a non-finite one too, which the exporter refuses but this lint judges
    (±Inf is OUT-OF-DOMAIN). A value whose suffix the exporter refuses, or
    whose number is outside the grammar (`1_000`, `0x1p4`, #3457), is not
    judged."""
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
        # Backtested at the number the exporter serves (#3267), with no
        # spelling table of this tool's own.
        assert got == (_number(row["value_part"]), row["severity"]), row
    else:
        assert got in ("not read", (80.0, "warning")), (row, got)


def _schema_accepts(row) -> bool:
    """What thresholdScalar's pattern can say of a row (#3457): the suffix is
    readable, and the value half is a number of the grammar or a disable
    word. Whether that number is finite, and that a threshold ROW (unlike a
    custom alert) refuses `disable:<severity>`, are the exporter's alone."""
    return row["ok"] and (row["grammar_ok"] or row["custom_alert_disabled"])


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_pattern_accepts_what_the_grammar_reads(row):
    assert (re.search(_pattern(), row["value"]) is not None) == _schema_accepts(row), row


@pytest.mark.parametrize("row", _rows(), ids=lambda r: repr(r["value"]))
def test_schema_validator_accepts_what_the_grammar_reads(row):
    jsonschema = pytest.importorskip("jsonschema")
    with open(_SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    wrapper = {"$ref": "#/definitions/thresholdScalar", "definitions": schema["definitions"]}
    assert jsonschema.Draft7Validator(wrapper).is_valid(row["value"]) == _schema_accepts(row), row


def test_schema_pattern_differs_from_the_exporter_only_where_a_pattern_cannot_say():
    """Every row the exporter serves or reads as a disable is schema-valid;
    a schema-valid row it does not is an overflow of the grammar (`1e999`)
    or a `disable:<severity>`."""
    pattern = re.compile(_pattern())
    extra = []
    for row in _rows():
        served = (row["ok"] and row["float_ok"]) or row["row_disabled"]
        valid = pattern.search(row["value"]) is not None
        assert valid or not served, row
        if valid and not served:
            extra.append(row["value"])
            assert (row["grammar_ok"] and not row["float_ok"]) or (
                row["custom_alert_disabled"] and ":" in row["value"]), row
    assert {"1e999", "disable:critical"} <= set(extra), extra


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


def test_matrix_carries_the_spellings_outside_the_grammar():
    """#3457: what Go's ParseFloat, JavaScript's Number or Python's float
    read and the grammar does not — `_` between digits, a hex float or
    integer, an octal or binary prefix, a mantissa of more than 20 digits
    (#3827) — and the trims the three languages do not share. Each is a row,
    so every reader checked against the matrix is held to the exporter on
    it."""
    rows = {r["value"]: r for r in _rows()}
    outside = ("1_000", "-1_0", "1e1_0", "0x1p4", "0x_1p4", "0x1.p1", "0X1P-2:critical", "1__0", "_1", "1_",
               "0x10", "0x1F", "0o17", "0b101", "0x1p_4", "0x1p99999", "\u0661\u0660", "Infinity",
               "123456789012345678901", "000000000000000000001", "0.12345678901234567890",
               _LONG_MANTISSA, "", "\ufeff70", "70\ufeff", "\u001c70", "70\u001f")
    for v in outside:
        assert rows[v]["ok"] and rows[v]["grammar_ok"] is False and rows[v]["float_ok"] is False, v
    inside = {"12345678901234567890": 12345678901234567890.0, "00000000000000000001": 1.0,
              "1.2345678901234567890": 1.234567890123456789, "+5": 5.0, "5.": 5.0, ".5": 0.5, "017": 17.0,
              "1e-400": 0.0, "\u008570": 70.0, "70\u0085": 70.0}
    for v, number in inside.items():
        assert rows[v]["ok"] and rows[v]["grammar_ok"] is True and rows[v]["float_ok"] is True, v
        assert _lib_validation.threshold_number(rows[v]["value_part"]) == number, v
    # In the grammar, not finite: the two columns are told apart.
    for v in ("1e999", "1e309", "-1e309"):
        assert rows[v]["grammar_ok"] is True and rows[v]["float_ok"] is False, v
    assert rows[":critical"]["ok"] and rows[":critical"]["grammar_ok"] is False


def test_matrix_header_is_written_by_go():
    """The header the generator composes the schema's pattern and the
    portal's trim from. U+0085 is trimmed and U+FEFF is not: the reverse of
    JavaScript's trim()."""
    m = _matrix()
    assert 0x85 in m["trim_code_points"] and 0xFEFF not in m["trim_code_points"]
    assert not {0x1C, 0x1D, 0x1E, 0x1F} & set(m["trim_code_points"]), "Python's strip() trims these, Go does not"
    assert m["trim_code_points"] == sorted(set(m["trim_code_points"]))
    assert set(m["disable_words"]) == set(_lib_validation._DISABLED_VALUES)
    assert set(m["disable_letters"]) == set("".join(m["disable_words"]))
    assert 0x130 in m["disable_letters"]["i"], "Go lower-cases U+0130 to i"


def test_matrix_carries_the_opt_out_samples():
    rows = {r["value"]: r for r in _rows()}
    for v in ("disable", "disable:warning", "disable:critical", "disable:bogus",
              "disable:", "DISABLE:CRITICAL", "off:warning", "false:critical"):
        assert "custom_alert_disabled" in rows[v], v
    assert {rows[v]["custom_alert_disabled"] for v in ("disable:critical", "disable:bogus")} == {True, False}
