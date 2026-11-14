"""#3764: a number in a number-typed field written other than in plain decimal
(`_lib_io.find_nondecimal_numbers`), refused by validate-config's
`yaml_quoting` row and by `check_confd_schema`.

yaml.v3 rewrites such a spelling on the way to the exporter's readers
(`min_events: 0x10` arrives as `16`), and `min_events` is judged by its text
(it enters the recipe_id slug; its validator takes plain decimal digits).
The owner ruling keeps the runtime as it reads and refuses the spelling at
the entry. As with the string-field rule, the verdict is parse-tree's tag
plus the schema's field type — no spelling list, no field list.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

import _lib_parse_tree as lpt  # noqa: E402
from _lib_io import find_misread_scalars, find_nondecimal_numbers  # noqa: E402

_TENANT = "tenant-config.schema.json"


@pytest.fixture(scope="module")
def schemas() -> dict:
    with open(os.path.join(_REPO, "docs", "schemas", _TENANT), encoding="utf-8") as fh:
        return {_TENANT: json.load(fh)}


@pytest.fixture(scope="module")
def nodes_of(tmp_path_factory):
    root = tmp_path_factory.mktemp("conf.d")

    def read(text: str):
        return lpt.load_file(root, "t.yaml", text.encode("utf-8"), None, 60).node("t.yaml")
    return read


def _min_events(token: str) -> str:
    return ("tenants:\n  t1:\n    _custom_alerts:\n"
            "      - {recipe: slo_burn_rate, name: s, metric: e, denominator_metric: r,"
            f" objective: \"99.9\", min_events: {token}}}\n")


_PATH = "/tenants/t1/_custom_alerts/0/min_events"


@pytest.mark.parametrize("token,value", [
    ("0x10", "16"), ("0o10", "8"), ("010", "8"), ("1_000", "1000"),
    ("+5", "5"), ("1e1", "10"), ("1.0", "1"), ("0b101", "5"), ("-0", "0"),
])
def test_a_rewritten_spelling_in_an_integer_field_is_reported(token, value, schemas, nodes_of):
    (hit,) = find_nondecimal_numbers(nodes_of(_min_events(token)), schemas[_TENANT], schemas, _TENANT)
    assert (hit.path, hit.text, hit.value, hit.integer) == (_PATH, token, value, True)
    assert f"#3764): min_events: {value}" in hit.message()


@pytest.mark.parametrize("token", ["20", "0", "-3", "\"20\"",
                                   # Not a value an integer field can hold:
                                   # the schema's type check reports these.
                                   "1.5", ".inf", "-.inf", ".nan"])
def test_a_plain_decimal_a_string_or_a_type_error_is_not(token, schemas, nodes_of):
    # A quoted "20" is a string: the schema's own type check refuses it,
    # not this rule.
    assert find_nondecimal_numbers(nodes_of(_min_events(token)), schemas[_TENANT], schemas, _TENANT) == []


def test_the_field_type_comes_from_the_schema(nodes_of):
    """The same nodes flip verdict with the schema alone: a `number` field
    takes `.5`-free decimals, a mixed or string field is not this rule's."""
    root = nodes_of("a: 1.50\nb: 1e3\nc: 0x1F\nd: 2.5\ne: .inf\n")

    def schema(t):
        return {"type": "object", "properties": {k: {"type": t} for k in "abcde"}}
    # A number field: 1.50 is 1.5 written with a trailing zero; 2.5 is plain;
    # .inf has no plain spelling (the schema's type check is its judge).
    nums = find_nondecimal_numbers(root, schema("number"))
    assert [(h.path, h.value) for h in nums] == [("/a", "1.5"), ("/b", "1000"), ("/c", "31")]
    # An integer field: 1.50, 2.5 and .inf are type errors, not spellings.
    assert [h.path for h in find_nondecimal_numbers(root, schema("integer"))] == ["/b", "/c"]
    assert find_nondecimal_numbers(root, schema(["integer", "string"])) == []
    assert find_nondecimal_numbers(root, schema("string")) == []
    # …which is the string-field rule's (#2164), not this one's.
    assert [h.path for h in find_misread_scalars(root, schema("string"))] == ["/a", "/b", "/c", "/d", "/e"]


def test_min_events_is_judged_through_the_shipped_schema(schemas, nodes_of):
    """The rule reads field types from the schema (no field list here); this
    pins that the shipped schema's min_events reaches it."""
    root = nodes_of(_min_events("0x10"))
    assert [h.path for h in find_nondecimal_numbers(root, schemas[_TENANT], schemas, _TENANT)] == [_PATH]


@pytest.mark.parametrize("entry", ["validate_config", "check_confd_schema"])
def test_both_entries_refuse_it(entry, tmp_path):
    """validate-config's yaml_quoting row and check_confd_schema both FAIL on
    it, with the #3764 line — the one rule, two entries."""
    guard = os.environ.get("DA_GUARD_BINARY")
    if not guard:
        pytest.skip("needs DA_GUARD_BINARY (da-guard parse-tree)")
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    (d / "t1.yaml").write_text(_min_events("0x10"), encoding="utf-8")
    script = {"validate_config": "scripts/tools/ops/validate_config.py",
              "check_confd_schema": "scripts/tools/lint/check_confd_schema.py"}[entry]
    r = subprocess.run([sys.executable, os.path.join(_REPO, script), "--config-dir", str(d)],
                       capture_output=True, text=True, encoding="utf-8", cwd=_REPO, timeout=300)
    out = r.stdout + r.stderr
    assert r.returncode != 0, out
    assert "'0x10' is read by yaml.v3" in out and "(#3764): min_events: 16" in out, out
    if entry == "validate_config":
        assert "[FAIL] yaml_quoting" in out, out
