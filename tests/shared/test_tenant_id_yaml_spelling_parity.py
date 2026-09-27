"""Python half of tests/shared/tenant_id_yaml_spelling_matrix.json (#2114).

`_lib_yaml_keys` must read a `tenants:` key as the tenant id the exporter
reads: the scalar's source text (yaml.v3 decoding into `map[string]…`), with a
null key dropped and a non-scalar key refused. The Go half
(components/threshold-exporter/app/pkg/config/tenant_id_spelling_parity_test.go)
asserts the same table through the exporter's own decode; this file asserts it
through both of this module's entry points.

It also owns the loader's SAFETY pin (moved here from
tests/ops/test_validate_config.py with the loader itself): bandit B506 cannot
see a directly constructed loader, so the property is pinned by behaviour.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))
import _lib_yaml_keys as yk  # noqa: E402

MATRIX = json.loads((Path(__file__).parent / "tenant_id_yaml_spelling_matrix.json")
                    .read_text(encoding="utf-8"))
ROWS = MATRIX["spellings"]
ACCEPTED = [r for r in ROWS if not r.get("rejected")]
REJECTED = [r for r in ROWS if r.get("rejected")]

LOADS = {
    "single": lambda s: yk.load_exporter_keys(io.StringIO(s)),
    "first": lambda s: yk.load_first_document_exporter_keys(io.StringIO(s)),
}


def _doc(source: str) -> str:
    return f'tenants:\n  {source}:\n    mysql_connections: "1"\n'


def test_matrix_shape_is_exact_and_not_vacuous() -> None:
    assert set(MATRIX) == {"_comment", "spellings"}
    assert len(ACCEPTED) == 22, len(ACCEPTED)
    assert REJECTED, "the non-scalar-key row is gone"
    for row in ROWS:
        assert {"source", "exporter_key", "safe_load_str"} <= set(row) <= {
            "source", "exporter_key", "safe_load_str", "rejected"}, row
        if row.get("rejected"):
            assert row["rejected"] is True
            assert row["exporter_key"] is None and row["safe_load_str"] is None, row
    assert len({r["source"] for r in ROWS}) == len(ROWS)


def test_the_table_separates_the_two_readers() -> None:
    """Anti-vacuity: the `safe_load_str` column is what PyYAML's typing gave
    (asserted, not trusted), and it disagrees with the exporter on 13 of the
    accepted rows — so a reader that fell back to `safe_load` would fail the
    table below. A rejected row is refused by `safe_load` too."""
    wrong = []
    for row in ACCEPTED:
        keys = list(yaml.safe_load(_doc(row["source"]))["tenants"])
        assert [str(k) for k in keys] == [row["safe_load_str"]], (row, keys)
        # A null key is still a (None) tenant to safe_load; the exporter has
        # none — a disagreement too.
        if row["exporter_key"] != str(keys[0]):
            wrong.append(row["source"])
    assert len(wrong) == 13, wrong
    for row in REJECTED:
        with pytest.raises(yaml.YAMLError):
            yaml.safe_load(_doc(row["source"]))


@pytest.mark.parametrize("how", sorted(LOADS))
@pytest.mark.parametrize("row", ACCEPTED, ids=lambda r: r["source"])
def test_every_entry_point_reads_the_exporters_key(row, how) -> None:
    tenants = LOADS[how](_doc(row["source"]))["tenants"]
    want = [] if row["exporter_key"] is None else [row["exporter_key"]]
    assert list(tenants) == want, (row, how, tenants)
    # Values keep PyYAML's typing: only KEYS are text.
    for body in tenants.values():
        assert body == {"mysql_connections": "1"}


@pytest.mark.parametrize("how", sorted(LOADS))
@pytest.mark.parametrize("row", REJECTED, ids=lambda r: r["source"])
def test_a_non_scalar_key_is_refused_as_the_exporter_refuses_it(row, how) -> None:
    """#2114 review: `? [a, b]` became a tenant named "[]" (str() of a list
    the constructor had not filled yet) — accepted in silence, where PyYAML
    raises `found unhashable key` and Go `cannot unmarshal !!seq into
    string`. It must be a yaml.YAMLError, so every reader's existing
    "unreadable file" handling names the file."""
    with pytest.raises(yaml.YAMLError, match="unhashable key"):
        LOADS[how](_doc(row["source"]))


@pytest.mark.parametrize("how", sorted(LOADS))
@pytest.mark.parametrize("src", [
    "tenants: !!map [a, b]\n",
    "tenants: !!map abc\n",
    "!!map [a]\n",
], ids=["tagged-sequence", "tagged-scalar", "tagged-root"])
def test_a_map_tag_on_a_non_mapping_is_a_yaml_error(src, how) -> None:
    """#2114 review: `construct_mapping` on a node that is not a mapping
    leaked TypeError / ValueError; `yaml.safe_load` raises a YAMLError there,
    and the readers' handlers catch exactly that (a policy-engine run went
    from rc 2 caller-error JSON to an rc 1 traceback)."""
    with pytest.raises(yaml.YAMLError, match="expected a mapping node"):
        LOADS[how](src)
    # Control: the same shape is a YAMLError to PyYAML itself.
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(src)


@pytest.mark.parametrize("how", sorted(LOADS))
def test_two_spellings_of_one_id_in_one_mapping_fold_silently(how) -> None:
    """⚠️ PINS A KNOWN RESIDUAL, NOT THE TARGET. `123:` and `"123":` in the
    SAME mapping are one key here (the last wins) — `safe_load` made them two
    tenants (123 and "123"), and Go rejects the file outright (`mapping key
    "123" already defined`). Duplicate-key rejection belongs to #2123's
    shared strict loader; when it lands this test must flip to expecting a
    yaml.YAMLError, which is the point of pinning it here."""
    got = LOADS[how]('tenants:\n  123:\n    a: 1\n  "123":\n    a: 2\n')
    assert got == {"tenants": {"123": {"a": 2}}}, got


@pytest.mark.parametrize("how", sorted(LOADS))
def test_merge_keys_are_expanded_because_the_exporter_expands_them(how) -> None:
    got = LOADS[how]("common: &c\n  db-m: {}\ntenants:\n  <<: *c\n  db-x: {}\n")
    assert list(got["tenants"]) == ["db-m", "db-x"], got


@pytest.mark.parametrize("how", sorted(LOADS))
def test_values_keep_pyyaml_types(how) -> None:
    """`_severity_dedup: off` stays False — the value planes are unchanged."""
    got = LOADS[how]("tenants:\n  010:\n    _severity_dedup: off\n    n: 010\n")
    assert got == {"tenants": {"010": {"_severity_dedup": False, "n": 8}}}, got


def test_raw_text_sequences_reads_tenant_id_lists_as_text() -> None:
    """`exclude_tenants: [010, yes, "123"]` names tenants "010", "yes", "123";
    PyYAML's typing reads 8, True, '123' (the control below)."""
    src = 'policies:\n  - name: r\n    exclude_tenants: [010, yes, "123", 0x1F]\n'
    got = yk.load_exporter_keys(io.StringIO(src),
                                raw_text_sequences={"exclude_tenants"})
    assert got["policies"][0]["exclude_tenants"] == ["010", "yes", "123", "0x1F"]
    # Control: without the opt-in the same list keeps PyYAML's values.
    plain = yk.load_exporter_keys(io.StringIO(src))
    assert plain["policies"][0]["exclude_tenants"] == [8, True, "123", 31]


def test_the_first_document_is_not_lost_to_a_later_one() -> None:
    src = "tenants:\n  010: {}\n---\n[\n"
    got = yk.load_first_document_exporter_keys(io.StringIO(src))
    assert got == {"tenants": {"010": {}}}
    assert yk.load_first_document_exporter_keys(io.StringIO("")) is None
    # Control: the single-document entry does refuse the same stream.
    with pytest.raises(yaml.YAMLError):
        yk.load_exporter_keys(io.StringIO(src))


@pytest.mark.parametrize("how", sorted(LOADS))
def test_the_parser_is_the_pure_one_the_readers_had(how) -> None:
    """#2114 review: every caller kept the pure-Python parser it used on main
    (libyaml is deliberately not used — see the module docstring, #2123).
    The trailing tab is the input the two parsers are KNOWN to disagree on
    (tests/shared/test_yaml_loader_is_libyaml.py): the pure parser refuses
    it. Were this loader on libyaml, it would load."""
    assert issubclass(yk.ExporterKeyLoader, yaml.SafeLoader)
    with pytest.raises(yaml.YAMLError):
        LOADS[how]("tenants:\n  tx:\n    cpu: 80\t\n")


@pytest.mark.parametrize("how", sorted(LOADS))
def test_the_loader_cannot_construct_python_objects(how) -> None:
    """⛔ The safety property, measured — not the spelling of the call.

    The custom loader exists to change how mapping KEYS are read; it must
    not have widened what YAML is allowed to construct.

    ⛔ No static check answers that. dev-rules §5 item 4 is enforced by
    bandit B506, which reads how the loader is NAMED, not what it can
    construct — a ``SafeLoader`` subclass is indistinguishable to it from
    ``yaml.UnsafeLoader``, and a directly constructed loader (what every
    entry point of ``_lib_yaml_keys`` does) is outside its predicate
    entirely. This test is the only thing pinning the property; it feeds the
    real payload. See the block at rule 4 in ``tests/shared/test_sast.py``."""
    assert issubclass(yk.ExporterKeyLoader, yaml.SafeLoader)
    payload = "tenants:\n  t: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(yaml.YAMLError):
        LOADS[how](payload)
    # Must-still-work control: an ordinary document still loads, so the
    # assertion above cannot be satisfied by a loader that refuses everything.
    ok = LOADS[how]("tenants:\n  t: {a: 1}\n")
    assert ok == {"tenants": {"t": {"a": 1}}}, ok
