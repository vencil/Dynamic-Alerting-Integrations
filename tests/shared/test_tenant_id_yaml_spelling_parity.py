"""Python half of tests/shared/tenant_id_yaml_spelling_matrix.json (#2114).

`_lib_yaml_keys` must read a `tenants:` key as the tenant id the exporter
reads: the scalar's source text (yaml.v3 decoding into `map[string]…`), with a
null key dropped. The Go half
(components/threshold-exporter/app/pkg/config/tenant_id_spelling_parity_test.go)
asserts the `exporter_key` column through the exporter's own decode; this
file asserts the same column through both of this module's loaders and both
of its entry points.

It also owns the loaders' SAFETY pin (moved here from
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

LOADS = {
    "fast-single": lambda s: yk.load_exporter_keys(io.StringIO(s)),
    "pure-single": lambda s: yk.load_exporter_keys(io.StringIO(s), pure=True),
    "fast-first": lambda s: yk.load_first_document_exporter_keys(io.StringIO(s)),
    "pure-first": lambda s: yk.load_first_document_exporter_keys(io.StringIO(s), pure=True),
}


def _doc(source: str) -> str:
    return f'tenants:\n  {source}:\n    mysql_connections: "1"\n'


def test_matrix_shape_is_exact_and_not_vacuous() -> None:
    assert set(MATRIX) == {"_comment", "spellings"}
    assert len(ROWS) == 22, len(ROWS)
    for row in ROWS:
        assert set(row) == {"source", "exporter_key", "safe_load_str"}, row
    assert len({r["source"] for r in ROWS}) == len(ROWS)


def test_the_table_separates_the_two_readers() -> None:
    """Anti-vacuity: the `safe_load_str` column is what PyYAML's typing gave
    (asserted, not trusted), and it disagrees with the exporter on 13 rows —
    so a reader that fell back to `safe_load` would fail the table below."""
    wrong = []
    for row in ROWS:
        keys = list(yaml.safe_load(_doc(row["source"]))["tenants"])
        assert [str(k) for k in keys] == [row["safe_load_str"]], (row, keys)
        # A null key is still a (None) tenant to safe_load; the exporter has
        # none — a disagreement too.
        if row["exporter_key"] != str(keys[0]):
            wrong.append(row["source"])
    assert len(wrong) == 13, wrong


@pytest.mark.parametrize("how", sorted(LOADS))
@pytest.mark.parametrize("row", ROWS, ids=lambda r: r["source"])
def test_every_loader_reads_the_exporters_key(row, how) -> None:
    tenants = LOADS[how](_doc(row["source"]))["tenants"]
    want = [] if row["exporter_key"] is None else [row["exporter_key"]]
    assert list(tenants) == want, (row, how, tenants)
    # Values keep PyYAML's typing: only KEYS are text.
    for body in tenants.values():
        assert body == {"mysql_connections": "1"}


@pytest.mark.parametrize("how", sorted(LOADS))
def test_merge_keys_are_expanded_because_the_exporter_expands_them(how) -> None:
    got = LOADS[how]("common: &c\n  db-m: {}\ntenants:\n  <<: *c\n  db-x: {}\n")
    assert list(got["tenants"]) == ["db-m", "db-x"], got


@pytest.mark.parametrize("how", sorted(LOADS))
def test_values_keep_pyyaml_types(how) -> None:
    """`_severity_dedup: off` stays False — the value planes are unchanged."""
    got = LOADS[how]("tenants:\n  010:\n    _severity_dedup: off\n    n: 010\n")
    assert got == {"tenants": {"010": {"_severity_dedup": False, "n": 8}}}, got


@pytest.mark.parametrize("pure", [False, True])
def test_raw_text_sequences_reads_tenant_id_lists_as_text(pure) -> None:
    """`exclude_tenants: [010, yes, "123"]` names tenants "010", "yes", "123";
    PyYAML's typing reads 8, True, '123' (the control below)."""
    src = 'policies:\n  - name: r\n    exclude_tenants: [010, yes, "123", 0x1F]\n'
    got = yk.load_exporter_keys(io.StringIO(src), pure=pure,
                                raw_text_sequences={"exclude_tenants"})
    assert got["policies"][0]["exclude_tenants"] == ["010", "yes", "123", "0x1F"]
    # Control: without the opt-in the same list keeps PyYAML's values.
    plain = yk.load_exporter_keys(io.StringIO(src), pure=pure)
    assert plain["policies"][0]["exclude_tenants"] == [8, True, "123", 31]


@pytest.mark.parametrize("pure", [False, True])
def test_the_first_document_is_not_lost_to_a_later_one(pure) -> None:
    src = "tenants:\n  010: {}\n---\n[\n"
    got = yk.load_first_document_exporter_keys(io.StringIO(src), pure=pure)
    assert got == {"tenants": {"010": {}}}
    assert yk.load_first_document_exporter_keys(io.StringIO(""), pure=pure) is None
    # Control: the single-document entry does refuse the same stream.
    with pytest.raises(yaml.YAMLError):
        yk.load_exporter_keys(io.StringIO(src), pure=pure)


@pytest.mark.parametrize("how", sorted(LOADS))
def test_the_loaders_cannot_construct_python_objects(how) -> None:
    """⛔ The safety property, measured — not the spelling of the call.

    The custom loaders exist to change how mapping KEYS are read; they must
    not have widened what YAML is allowed to construct.

    ⛔ No static check answers that. dev-rules §5 item 4 is enforced by
    bandit B506, which reads how the loader is NAMED, not what it can
    construct — a ``SafeLoader`` subclass is indistinguishable to it from
    ``yaml.UnsafeLoader``, and a directly constructed loader (what every
    entry point of ``_lib_yaml_keys`` does) is outside its predicate
    entirely. This test is the only thing pinning the property; it feeds the
    real payload. See the block at rule 4 in ``tests/shared/test_sast.py``."""
    safe_bases = (yaml.SafeLoader, getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    assert issubclass(yk.ExporterKeyLoader, safe_bases)
    assert issubclass(yk.PureExporterKeyLoader, yaml.SafeLoader)
    payload = "tenants:\n  t: !!python/object/apply:os.system ['echo pwned']\n"
    with pytest.raises(yaml.YAMLError):
        LOADS[how](payload)
    # Must-still-work control: an ordinary document still loads, so the
    # assertion above cannot be satisfied by a loader that refuses everything.
    ok = LOADS[how]("tenants:\n  t: {a: 1}\n")
    assert ok == {"tenants": {"t": {"a": 1}}}, ok
