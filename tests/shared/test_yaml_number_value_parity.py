"""Python half of tests/shared/yaml_number_value_matrix.json (#2415).

describe_tenant must show an int / float VALUE as the exporter's yaml.v3
reads it, and the exporter's merged_hash over it. Since #2906 every value it
prints is `da-guard effective`'s, so this half holds the CLI's output to the
`go_json` column — pkg/config's CanonicalJSON of the same tree, asserted on
every Go run by
components/threshold-exporter/app/pkg/config/yaml_number_value_parity_test.go
— and never reads the Go source. The rows describe_tenant's own PyYAML
reader could not align (once strict xfails) are held to it like the rest.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _go_refusal import assert_refused, go_refused  # noqa: E402  (#3206)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"
MATRIX = json.loads((Path(__file__).parent / "yaml_number_value_matrix.json")
                    .read_text(encoding="utf-8"))
ROWS = MATRIX["rows"]
DEFAULTS = "defaults:\n  mysql_connections: 50\n"

pytestmark = pytest.mark.usefixtures("da_guard_env")


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


def _describe(conf_d: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(DESCRIBE), *args, "--conf-d", str(conf_d)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_matrix_shape_is_exact_and_not_vacuous() -> None:
    assert set(MATRIX) == {"_comment", "rows"}
    assert len(ROWS) >= 100, len(ROWS)
    sources = [r["source"] for r in ROWS]
    assert len(sources) == len(set(sources)), "a source is listed twice"
    for row in ROWS:
        assert {"source", "go_json"} <= set(row) <= {"source", "go_json", "go_type"}
    # The issue's own rows, and the ones it lists as already aligned.
    for source in ("1.0", "0o17", "12:30:45", "1_2:30", "1e3", "42", '"0o17"', "abc",
                   ".5", "0x1F", "+12", "1_000", "0b101", ".inf", ".nan"):
        assert source in sources, source
    # #2164: the YAML 1.1 bool spellings are out of this table's scope.
    assert not {"yes", "no", "on", "off"} & set(sources)


@pytest.mark.parametrize("side", ["tenant", "defaults"])
@pytest.mark.parametrize("row", ROWS, ids=[r["source"] for r in ROWS])
def test_describe_tenant_shows_the_exporters_value_and_hash(tmp_path: Path, row: dict, side: str) -> None:
    conf_d = _tree(tmp_path, row["source"], side)
    res = _describe(conf_d, "t1", "--show-sources")
    assert "Traceback" not in res.stderr, res.stderr
    go = json.loads(subprocess.run(
        [os.environ["DA_GUARD_BINARY"], "effective", "--config-dir", str(conf_d)],
        capture_output=True, text=True, encoding="utf-8", timeout=60).stdout)
    refused = go_refused(conf_d)
    if refused:
        # A file yaml.v3 refuses, or one the exporter drops for a value it
        # cannot serve (a non-finite root default): Go names it, so the run
        # is refused, named, nothing described (#3206).
        assert refused == [{"tenant": "t1.yaml", "defaults": "_defaults.yaml"}[side]], refused
        assert_refused(res.returncode, res.stderr, refused)
        assert res.stdout == ""
        return
    assert row["go_json"] is not None, "yaml.v3 refuses this file, yet Go names no path"
    assert not go["parse_failed"], go
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["effective_config"] == _as_sent(json.loads(row["go_json"]))
    assert out["merged_hash"] == _hash(row["go_json"]), out


def _as_sent(value):
    """`go_json` (CanonicalJSON: bare NaN / ±Inf) as /effective sends it: a
    non-finite number as the text "NaN" / "Infinity" / "-Infinity"."""
    if isinstance(value, dict):
        return {k: _as_sent(v) for k, v in value.items()}
    if isinstance(value, float) and value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "Infinity" if value > 0 else "-Infinity"
    return value


# The issue's five divergent rows, end to end through the CLI, with the
# merged_hash measured on the Go side and the value's JSON type.
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
    res = _describe(conf_d, "t1", "--show-sources")
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    value, merged_hash = ISSUE_ROWS[source]
    assert out["merged_hash"] == merged_hash
    got = out["effective_config"]["_x"]["v"]
    assert got == value and type(got) is type(value), (got, value)
    row = next(r for r in ROWS if r["source"] == source)
    assert _hash(row["go_json"]) == merged_hash


def test_yaml_one_one_bools_are_the_exporters(tmp_path: Path) -> None:
    """Scope guard (#2164): a plain `yes` is a string to yaml.v3, and the
    value shown is the exporter's (this tool's PyYAML reader made it True)."""
    res = _describe(_tree(tmp_path, "yes"), "t1")
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["effective_config"]["_x"]["v"] == "yes"
