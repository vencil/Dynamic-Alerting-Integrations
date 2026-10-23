"""ONE defaults carrier per directory, as the Python readers see it (#1674, B8).

The rule is Go's alone (`pkg/config.SelectDefaultsCarriers`, its table in
components/threshold-exporter/app/pkg/config/defaults_carriers_test.go): the
Python readers take the carrier from `da-guard parse-tree`'s
`defaults_carriers` / `not_read` (#3206 — `_lib_confd.select_defaults_carrier`,
the Python restatement this file used to hold to that table row for row, was
deleted with the other name rules). What is left here is end to end: the
carrier a Python reader edits and reports is the one Go selects.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _platform_fs import symlink_or_skip  # noqa: E402
from _go_refusal import assert_refused  # noqa: E402  (#3206)

from _lib_confd import resolve_defaults_file

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIBE = REPO_ROOT / "scripts" / "tools" / "dx" / "describe_tenant.py"

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
    # Since #2906 describe_tenant has no carrier selection of its own: the
    # warning is parse-tree's `not_read` verdict on the carrier no plane
    # reads (#3147; said by describe_tenant itself since #3230 removed the
    # custom-alert loader that said it before).
    warn = "_defaults.yml is ignored on every plane (#1674)"
    assert warn in r.stderr, r.stderr
    # Once per file per run (blind review).
    assert r.stderr.count(warn) == 1, r.stderr


def _symlink_or_skip(link: Path, target: Path) -> None:
    symlink_or_skip(target, link)


def test_describe_tenant_selects_a_symlinked_carrier_by_its_entry_name(
        tmp_path: Path) -> None:
    """Blind review of #1674: selecting on the RESOLVED name made
    `_defaults.yaml -> sub/platform-base.yaml` select nothing and crash with
    AttributeError (rc 1); before #1674 it was rc 0 with this chain. Go's
    walker never follows the link, so the entry name is what it classifies
    — and, since #2906 shows Go's chain as is, what it reports."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "platform-base.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
    (sub / "t.yaml").write_text("tenants:\n  t-link: {}\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / "_defaults.yaml", Path("sub") / "platform-base.yaml")
    r = _describe(tmp_path, "t-link")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["defaults_chain"] == ["_defaults.yaml"]
    assert out["effective_config"] == {"cpu_pct": 50}


def test_describe_tenant_refuses_a_tree_with_a_dangling_carrier(tmp_path: Path) -> None:
    """A carrier the walker cannot read is not a candidate (the readable
    `.yml` is the exporter's carrier), but the dangling `.yaml` is a path da-guard
    validation names (exit 3): describe_tenant refuses the tree, naming it
    (the shared conf.d refusal, #3206)."""
    (tmp_path / "_defaults.yml").write_text("defaults:\n  cpu_pct: 70\n", encoding="utf-8")
    _symlink_or_skip(tmp_path / "_defaults.yaml", tmp_path / "missing.yaml")
    (tmp_path / "t.yaml").write_text("tenants:\n  t-dang: {}\n", encoding="utf-8")
    r = _describe(tmp_path, "t-dang")
    assert_refused(r.returncode, r.stderr, ["_defaults.yaml"])
    assert "(read_failure: " in r.stderr, r.stderr


def test_describe_tenant_single_carrier_is_silent(tmp_path: Path) -> None:
    """Counterfactual for the WARN above: one carrier, no warning."""
    (tmp_path / "_defaults.yaml").write_text("defaults:\n  cpu_pct: 50\n", encoding="utf-8")
    (tmp_path / "t.yaml").write_text("tenants:\n  t-one: {}\n", encoding="utf-8")
    r = _describe(tmp_path, "t-one")
    assert r.returncode == 0, r.stderr
    assert "defaults carriers" not in r.stderr, r.stderr
