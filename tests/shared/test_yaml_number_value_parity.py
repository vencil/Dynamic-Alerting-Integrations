"""Python half of tests/shared/yaml_number_value_matrix.json (#2415).

describe_tenant must read an int / float VALUE as the exporter's yaml.v3
reads it and write it into the canonical JSON as encoding/json does, so its
merged_hash is the exporter's. The `go_json` column is pkg/config's
CanonicalJSON of the same tree, asserted on every Go run by
components/threshold-exporter/app/pkg/config/yaml_number_value_parity_test.go;
this file asserts describe_tenant against it and never reads the Go source.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "dx"))
import describe_tenant as dt  # noqa: E402

DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
MATRIX = json.loads((Path(__file__).parent / "yaml_number_value_matrix.json")
                    .read_text(encoding="utf-8"))
ROWS = MATRIX["rows"]
DEFAULTS = "defaults:\n  mysql_connections: 50\n"


def _tree(tmp_path: Path, source: str, side: str = "tenant") -> Path:
    """The row's value in the tenant file, or in `_defaults.yaml` under an
    empty tenant — one effective config either way (the Go half builds the
    same two trees)."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    if side == "tenant":
        defaults, tenant = DEFAULTS, f"tenants:\n  t1:\n    _x:\n      v: {source}\n"
    else:
        defaults, tenant = DEFAULTS + f"  _x:\n    v: {source}\n", "tenants:\n  t1: {}\n"
    (conf_d / "_defaults.yaml").write_text(defaults, encoding="utf-8")
    (conf_d / "t1.yaml").write_text(tenant, encoding="utf-8")
    return conf_d


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def test_matrix_shape_is_exact_and_not_vacuous() -> None:
    assert set(MATRIX) == {"_comment", "rows"}
    assert len(ROWS) >= 100, len(ROWS)
    sources = [r["source"] for r in ROWS]
    assert len(sources) == len(set(sources)), "a source is listed twice"
    for row in ROWS:
        assert {"source", "go_json"} <= set(row) <= {"source", "go_json", "go_type", "py_divergent"}
    # The issue's own rows, and the ones it lists as already aligned.
    for source in ("1.0", "0o17", "12:30:45", "1_2:30", "1e3", "42", '"0o17"', "abc",
                   ".5", "0x1F", "+12", "1_000", "0b101", ".inf", ".nan"):
        assert source in sources, source
    # #2164: the YAML 1.1 bool spellings are out of this table's scope.
    assert not {"yes", "no", "on", "off"} & set(sources)


def _param(row: dict):
    marks = ()
    if row.get("py_divergent"):
        marks = pytest.mark.xfail(strict=True, reason=f"#2415 known divergence: {row['py_divergent']}")
    return pytest.param(row, id=row["source"], marks=marks)


@pytest.mark.parametrize("side", ["tenant", "defaults"])
@pytest.mark.parametrize("row", [_param(r) for r in ROWS])
def test_describe_tenant_matches_the_exporter(tmp_path: Path, row: dict, side: str) -> None:
    conf_d = _tree(tmp_path, row["source"], side)
    if row["go_json"] is None and side == "defaults":
        # yaml.v3 refuses the file; a defaults level cannot be dropped (#2413).
        with pytest.raises(dt.DefaultsParseError):
            dt.ConfDScanner(conf_d)
        return
    scanner = dt.ConfDScanner(conf_d)
    if row["go_json"] is None:  # yaml.v3 refuses the file: no tenant t1 there either
        with pytest.raises(KeyError):
            scanner.effective_config("t1")
        return
    eff = scanner.effective_config("t1")
    assert dt._canonical_json(eff) == row["go_json"]
    assert scanner.source_info("t1")["merged_hash"] == _hash(row["go_json"])


# The issue's five divergent rows, end to end through the CLI: the printed
# effective_config and merged_hash are the exporter's.
ISSUE_ROWS = {
    "1.0": (1, "5447b9dcc066b19a"),
    "0o17": (15, "a68f3ce1a63b337d"),
    "12:30:45": ("12:30:45", "d4f79575ddbf404a"),
    "1_2:30": ("1_2:30", "924c76cb45e897fe"),
    "1e3": (1000, "d041446c0445886e"),
}


@pytest.mark.parametrize("source", sorted(ISSUE_ROWS))
def test_cli_prints_the_exporters_value_and_hash(tmp_path: Path, source: str) -> None:
    conf_d = _tree(tmp_path, source)
    res = subprocess.run(
        [sys.executable, str(DESCRIBE), "t1", "--conf-d", str(conf_d), "--show-sources"],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    value, merged_hash = ISSUE_ROWS[source]
    assert out["merged_hash"] == merged_hash
    got = out["effective_config"]["_x"]["v"]
    assert got == value and type(got) is type(value), (got, value)
    # encoding/json's text in the output too: `1`, not `1.0`.
    row = next(r for r in ROWS if r["source"] == source)
    assert _hash(row["go_json"]) == merged_hash


@pytest.mark.parametrize("value,text", [
    (1.0, "1"), (-0.0, "-0"), (5.0, "5"), (1e20, "100000000000000000000"),
    (1e21, "1e+21"), (1.5e22, "1.5e+22"), (1.5e-5, "0.000015"), (1e-6, "0.000001"),
    (1e-7, "1e-7"), (1.5e-7, "1.5e-7"), (1e-10, "1e-10"), (2.5e-4, "0.00025"),
    (123456789012345678.0, "123456789012345680"), (0.30000000000000004, "0.30000000000000004"),
])
def test_float_text_is_encoding_json(value: float, text: str) -> None:
    """encoding/json's float text, unit by unit (the matrix rows carry the
    Go measurement for the same values; `1e-10` is a two-digit negative
    exponent, which encoding/json does not shorten)."""
    assert dt._json_text({"v": value}, separators=(",", ":")) == '{"v":' + text + "}"


def test_yaml_one_one_bools_are_untouched(tmp_path: Path) -> None:
    """Scope guard: #2164 kept `yes` / `on` as PyYAML's bools on the READ
    side; #2415 changes int / float only."""
    scanner = dt.ConfDScanner(_tree(tmp_path, "yes"))
    assert scanner.effective_config("t1")["_x"]["v"] is True
