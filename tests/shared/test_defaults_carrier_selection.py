"""ONE defaults carrier per directory, in Python (#1674, B8).

`_lib_confd.select_defaults_carrier` restates the Go rule
(`pkg/config.SelectDefaultsCarriers`) that every exporter plane reads. The
rows below are the Go table in
components/threshold-exporter/app/pkg/config/defaults_carriers_test.go,
row for row; the golden fixtures (tests/golden, `carrier-selection-*`) pin
the two languages to one chain and one merged_hash on a real tree.

Neither side reads the other's source (#1448): each asserts these rows.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from _lib_confd import (
    multi_carrier_warning,
    resolve_defaults_file,
    select_defaults_carrier,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"

_ROWS = [
    ("single lower-case", ["_defaults.yaml"], "_defaults.yaml"),
    ("single upper-case .yaml is a carrier", ["_DEFAULTS.YAML"], "_DEFAULTS.YAML"),
    ("single upper-case .yml is a carrier", ["_DEFAULTS.YML"], "_DEFAULTS.YML"),
    (".yaml beats .yml (lower-case pair)", ["_defaults.yaml", "_defaults.yml"], "_defaults.yaml"),
    (".yaml beats .yml even when the .yml sorts first", ["_DEFAULTS.YML", "_defaults.yaml"], "_defaults.yaml"),
    ("folded .yaml beats lower-case .yml", ["_DEFAULTS.YAML", "_defaults.yml"], "_DEFAULTS.YAML"),
    # ⚠️ THE ASYMMETRY the owner ruling keeps: LAST .yaml, FIRST .yml.
    ("two .yaml case variants: the LAST in walk order", ["_Defaults.yaml", "_defaults.YAML"], "_defaults.YAML"),
    ("two .yml case variants: the FIRST in walk order", ["_DEFAULTS.YML", "_defaults.yml"], "_DEFAULTS.YML"),
]


@pytest.mark.parametrize("names,want", [(n, w) for _, n, w in _ROWS],
                         ids=[r[0] for r in _ROWS])
def test_selection_matches_the_go_table(names: list[str], want: str) -> None:
    # Both input orders: the rule reads NAME order, never the caller's.
    for ordering in (names, list(reversed(names))):
        got = select_defaults_carrier(Path("/conf.d") / n for n in ordering)
        assert got is not None and got.name == want, (ordering, got)


def test_no_carrier_selects_nothing() -> None:
    assert select_defaults_carrier([]) is None


def test_warning_names_the_chosen_and_the_ignored_file() -> None:
    msg = multi_carrier_warning("/conf.d", [Path("/conf.d/_defaults.yml"),
                                            Path("/conf.d/_defaults.yaml")])
    assert msg is not None
    for frag in ("WARN:", "/conf.d", "2 defaults carriers",
                 "_defaults.yaml, _defaults.yml", "only _defaults.yaml is read",
                 "_defaults.yml is ignored"):
        assert frag in msg, (frag, msg)
    assert multi_carrier_warning("/conf.d", [Path("/conf.d/_defaults.yaml")]) is None


def test_resolve_defaults_file_returns_the_carrier_the_exporter_reads(
        tmp_path: Path) -> None:
    """Before #1674 the first in sort order won — `_DEFAULTS.YML` over the
    `_defaults.yaml` the exporter merges, so a writer edited a dead file."""
    (tmp_path / "_DEFAULTS.YML").write_text("defaults: {}\n", encoding="utf-8")
    (tmp_path / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
    names = {p.name for p in tmp_path.iterdir()}
    if len(names) < 2:
        pytest.skip("case-insensitive filesystem: the two spellings collapse "
                    "into one file, so the selection cannot be measured here")
    assert resolve_defaults_file(tmp_path).name == "_defaults.yaml"


def _describe(conf_d: Path, tenant: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DESCRIBE), tenant, "--conf-d", str(conf_d),
         "--show-sources", "--format", "json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)


def test_describe_tenant_reads_one_carrier_and_warns(tmp_path: Path) -> None:
    """#1674 measurement B, Python leg: the chain was
    ['_defaults.yml', '_defaults.yaml'] and the effective config merged both
    ({cpu_pct: 50, mem_pct: 70, disk_pct: 4})."""
    (tmp_path / "_defaults.yaml").write_text(
        "defaults:\n  cpu_pct: 50\n  disk_pct: 4\n", encoding="utf-8")
    (tmp_path / "_defaults.yml").write_text(
        "defaults:\n  cpu_pct: 90\n  mem_pct: 70\n", encoding="utf-8")
    (tmp_path / "t.yaml").write_text("tenants:\n  t-pair: {}\n", encoding="utf-8")

    r = _describe(tmp_path, "t-pair")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["defaults_chain"] == ["_defaults.yaml"]
    assert out["effective_config"] == {"cpu_pct": 50, "disk_pct": 4}
    assert "defaults carriers" in r.stderr and "_defaults.yml is ignored" in r.stderr, r.stderr


def test_describe_tenant_single_carrier_is_silent(tmp_path: Path) -> None:
    """Counterfactual for the WARN above: one carrier, no warning."""
    (tmp_path / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
    (tmp_path / "t.yaml").write_text("tenants:\n  t-one: {}\n", encoding="utf-8")
    r = _describe(tmp_path, "t-one")
    assert r.returncode == 0, r.stderr
    assert "defaults carriers" not in r.stderr, r.stderr
