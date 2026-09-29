"""The route generator's duplicate-key verdict, as a table the Go side reads
(#2295).

The generator reads conf.d with `_lib_io`'s StrictLoader, which refuses the
WHOLE file when any mapping in it writes a key twice. da-guard and tenant-api
judge receivers the way the generator reads them, so they must refuse the
same files: `pkg/pyyamlcompat.FindDuplicateKey` copies the rule, and its test
(`duplicate_test.go`) holds it to this table row by row.

Each row's verdict is the StrictLoader's own, run here — never written by
hand. Regenerate with REGEN_PYYAML_DUPLICATES=1 when `_lib_io` or the
candidates change.
"""
from __future__ import annotations

import json
import os

import pytest
import yaml

from _lib_io import DuplicateKeyError, strict_load_all_exporter_keys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TABLE = os.path.join(_REPO_ROOT, "components", "threshold-exporter", "app", "pkg",
                      "pyyamlcompat", "testdata", "pyyaml_duplicate_keys.json")

# (name, document). Every document parses in both PyYAML and yaml.v3; the
# verdict column comes from _verdict below.
_CANDIDATES = [
    ("plain repeat", "a: 1\na: 2\n"),
    ("plain repeat nested", "t:\n  r:\n    x: 1\n    y: 2\n    x: 3\n"),
    ("repeat in list item", "l:\n  - {p: 1, p: 2}\n"),
    ("repeat in flow mapping", "m: {a: 1, b: 2, a: 3}\n"),
    ("alias key beside its anchor", "&k a : 1\n*k : 2\n"),
    ("alias key beside its anchor, nested", "t:\n  &r receiver : x\n  *r : y\n"),
    ("alias key anchored elsewhere", "ids: [&t rt]\ntenants:\n  rt: 1\n  *t : 2\n"),
    ("alias key alone", "ids: [&t rt]\ntenants:\n  *t : 1\n"),
    ("two alias keys of one anchor", "ids: [&t rt]\nm:\n  *t : 1\n  *t : 2\n"),
    ("alias key to a mapping anchor", "a: &m {x: 1}\nb:\n  ? *m\n  : 1\n  ? {y: 2}\n  : 2\n"),
    ("two mapping keys", "b:\n  ? {x: 1}\n  : 1\n  ? {y: 2}\n  : 2\n"),
    ("two list keys", "b:\n  ? [x]\n  : 1\n  ? [y]\n  : 2\n"),
    ("a mapping key and a list key", "b:\n  ? {x: 1}\n  : 1\n  ? [y]\n  : 2\n"),
    ("a scalar key and a mapping key", "b:\n  ? x\n  : 1\n  ? {x: 1}\n  : 2\n"),
    ("repeat inside a mapping key", "b:\n  ? {x: 1, x: 2}\n  : 1\n"),
    ("merge brings a name the mapping writes", "a: &x {p: 1}\nc:\n  <<: *x\n  p: 2\n"),
    ("merge list brings one name twice", "a: &x {p: 1}\nb: &y {p: 2}\nc:\n  <<: [*x, *y]\n"),
    ("merge source repeats a key", "a: &x\n  p: 1\n  p: 2\nc:\n  <<: *x\n"),
    ("merge key twice", "a: &x {p: 1}\nb: &y {q: 1}\nc:\n  <<: *x\n  <<: *y\n"),
    ("quoted and plain merge key", "a: &x {p: 1}\nc:\n  '<<': 1\n  <<: *x\n"),
    ("tagged and plain merge key", "a: &x {p: 1}\nb: &y {q: 1}\nc:\n  !!merge <<: *x\n  <<: *y\n"),
    ("inline merge value repeats", "c:\n  <<: {p: 1, p: 2}\n"),
    ("same name in different mappings", "a:\n  x: 1\nb:\n  x: 2\n"),
    ("same name at two depths", "x:\n  x:\n    x: 1\n"),
    ("1 and '1'", "m:\n  1: a\n  '1': b\n"),
    ("1 and \"1\"", "m:\n  1: a\n  \"1\": b\n"),
    ("1 and 01", "m:\n  1: a\n  01: b\n"),
    ("true and 'true'", "m:\n  true: a\n  'true': b\n"),
    ("true and True", "m:\n  true: a\n  True: b\n"),
    ("~ and null", "m:\n  ~: 1\n  null: 2\n"),
    ("!!str and plain", "m:\n  !!str a: 1\n  a: 2\n"),
    ("empty key twice", "m:\n  ? \n  : 1\n  '': 2\n"),
    ("escaped and plain", "m:\n  \"\\x41\": 1\n  A: 2\n"),
    ("folded key and plain", "m:\n  ? >-\n    a\n  : 1\n  a: 2\n"),
    ("multi-document, repeat in the second", "a: 1\n---\nb: 1\nb: 2\n"),
    ("multi-document, same key in each", "a: 1\n---\na: 2\n"),
    ("self-referencing anchor", "a: &x\n  b: *x\n  c: 1\n"),
    ("self-referencing anchor with a repeat", "a: &x\n  b: *x\n  c: 1\n  c: 2\n"),
    ("alias fan-out", "a: &x {p: 1, q: 2}\nb: [*x, *x, *x, *x]\nc: {k1: *x, k2: *x}\n"),
    ("tenant file, alias receiver twice",
     "tenants:\n  rt:\n    _routing:\n      &r receiver : {type: webhook}\n      *r : {type: email}\n"),
    ("tenant file, merge override", "tenants:\n  ra: &b\n    x: '1'\n  rt:\n    <<: *b\n    x: '2'\n"),
    ("domain policy, alias constraints twice",
     "domain_policies:\n  d1:\n    tenants: [rt]\n    &c constraints :\n      forbidden_receiver_types: [webhook]\n"
     "    *c :\n      forbidden_receiver_types: [webhook]\n"),
]


def _verdict(doc: str) -> "str | None":
    """The repeated key the StrictLoader reports (every document checked),
    or None when its duplicate check passes. A stream it then fails to
    CONSTRUCT (a mapping used as a key is unhashable) passed that check: the
    file is refused, but not for a duplicate, and that is not this rule."""
    try:
        list(strict_load_all_exporter_keys(doc))
    except DuplicateKeyError as exc:
        return exc.key
    except yaml.YAMLError:
        return None
    return None


def _table() -> list[dict]:
    return [{"name": n, "yaml": d, "duplicate": _verdict(d)} for n, d in _CANDIDATES]


def test_duplicate_key_table_is_the_strict_loaders_verdict():
    table = _table()
    if os.environ.get("REGEN_PYYAML_DUPLICATES"):
        with open(_TABLE, "w", encoding="utf-8") as fh:
            fh.write("[\n" + ",\n".join(json.dumps(r, ensure_ascii=False) for r in table) + "\n]\n")
    with open(_TABLE, encoding="utf-8") as fh:
        committed = json.load(fh)
    assert committed == table, (
        "pyyaml_duplicate_keys.json differs from the StrictLoader's verdict; "
        "rerun with REGEN_PYYAML_DUPLICATES=1")


def test_table_has_both_verdicts():
    verdicts = [r["duplicate"] is None for r in _table()]
    assert any(verdicts) and not all(verdicts)


@pytest.mark.parametrize("name,doc", _CANDIDATES, ids=[c[0] for c in _CANDIDATES])
def test_a_refused_row_is_refused_by_the_generators_reader(name, doc):
    """The single-document reader the generator uses refuses every row the
    table marks (a duplicate, or — for more than one document — the stream
    itself), so the table's verdict is never looser than the generator."""
    from _lib_io import strict_load_exporter_keys

    if _verdict(doc) is None:
        return
    with pytest.raises(yaml.YAMLError):
        strict_load_exporter_keys(doc)
