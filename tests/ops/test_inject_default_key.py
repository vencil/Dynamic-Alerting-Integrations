"""Tests for scripts/ops/inject_default_key.py.

Use case: bench harness orchestrator's idempotent `bench_trigger=50`
injection into `_defaults.yaml` (Track A A6 helper, replaces the inline
Python in bench_e2e_run.sh).

Coverage focuses on:
  - Numeric guard (cycle-6 RCA contract)
  - Idempotence (re-run after generator already wrote the key)
  - Three-way file state (exists+has-key / exists+no-key / missing)
  - main() exit-code contract
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = REPO_ROOT / "scripts" / "ops" / "inject_default_key.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("inject_default_key", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["inject_default_key"] = m
    spec.loader.exec_module(m)
    return m


def test_inject_into_existing_defaults_block(mod, tmp_path):
    """Standard generator-output shape: `defaults:` block exists, key absent."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert "bench_trigger: 50" in text
    assert "mysql_connections: 80" in text  # original preserved


def test_inject_idempotent_when_key_already_present(mod, tmp_path):
    """Re-run safe: key already there → no change, exit 0."""
    p = tmp_path / "_defaults.yaml"
    p.write_text(
        "defaults:\n  mysql_connections: 80\n  bench_trigger: 50\n",
        encoding="utf-8",
    )
    before = p.read_text(encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    assert p.read_text(encoding="utf-8") == before  # bytewise identical


def test_inject_creates_file_when_missing(mod, tmp_path):
    """Customer-anon fixture without `_defaults.yaml` → create one."""
    p = tmp_path / "subdir" / "_defaults.yaml"  # nested missing
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    assert text.startswith("defaults:")
    assert "bench_trigger: 50" in text


def test_inject_creates_defaults_block_in_existing_yaml_without_one(mod, tmp_path):
    """File has `tenants:` etc. but no `defaults:` block → prepend one."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("# Some other top-level\ntenants: {}\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "50")
    assert rc == 0
    text = p.read_text(encoding="utf-8")
    assert text.startswith("defaults:")
    assert "  bench_trigger: 50" in text
    assert "tenants: {}" in text  # original preserved


def test_inject_rejects_non_numeric(mod, tmp_path):
    """Cycle-6 contract: non-numeric values reject."""
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    rc = mod.inject(p, "bench_trigger", "X:critical")
    assert rc == 1
    # File untouched
    assert p.read_text(encoding="utf-8") == "defaults:\n"


def test_inject_accepts_int_string(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "k", "42") == 0


def test_inject_accepts_float_string(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    assert mod.inject(p, "k", "1.5") == 0
    assert "k: 1.5" in p.read_text(encoding="utf-8")


def test_main_usage_error_returns_1(mod, capsys):
    rc = mod.main(["inject_default_key.py"])
    assert rc == 1
    assert "usage:" in capsys.readouterr().err


def test_main_too_many_args_returns_1(mod, capsys):
    rc = mod.main(["inject_default_key.py", "a", "b", "c", "d"])
    assert rc == 1


def test_main_happy_path_returns_0(mod, tmp_path):
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n", encoding="utf-8")
    rc = mod.main(
        ["inject_default_key.py", str(p), "bench_trigger", "50"]
    )
    assert rc == 0


# ── The inert-shape refusal (#1412) ─────────────────────────────────────────
#
# ⛔ A different question from the numeric guard above, and the reason both
# exist: `mysql_connections_critical: 90` passes every check this tool had. It
# is numeric, it is a valid YAML key, the file still parses — and under
# `defaults:` the exporter never looks at it (`resolveCriticalRows` reads the
# critical tier from tenant overrides keyed on `defaults[<base>]`;
# `resolveDimensionalRows` is tenant-only). No parse error, no WARN, no series.
#
# This tool is also why the refusal lives HERE rather than in a downstream gate:
# it writes into fixture trees that no `check_threshold_reachability` face reads,
# so it was one of the two producers #1412 found invisible. The census in that
# gate exempts this module BY NAMING this refusal — so if these tests go, the
# exemption is a claim with nothing behind it.
@pytest.mark.parametrize("key", [
    "mysql_connections_critical",
    "pg_replication_lag_critical",
    "es_disk_usage_percent{index=main}",
])
def test_inert_default_shapes_are_refused(mod, tmp_path, key, capsys):
    p = tmp_path / "_defaults.yaml"
    before = "defaults:\n  mysql_connections: 80\n"
    p.write_text(before, encoding="utf-8")

    rc = mod.inject(p, key, "90")

    assert rc == 1, f"{key!r} was accepted under `defaults:`"
    # ⛔ The file is UNCHANGED. A refusal that still wrote would be worse than no
    # refusal: the operator reads an error and the inert key ships anyway.
    assert p.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "fails SILENTLY" in err
    assert "<tenant>.yaml" in err, (
        "the refusal must say where the tier DOES belong; an error that only "
        "says no leaves the operator to guess"
    )


def test_the_base_key_of_a_refused_tier_is_still_accepted(mod, tmp_path):
    """The refusal is about the SHAPE, not about the metric.

    Without this cell a tool that refused every key would satisfy the test above
    — the "must-fire" control has to be paired with a "must-not-fire" one.
    """
    p = tmp_path / "_defaults.yaml"
    p.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    assert mod.inject(p, "pg_replication_lag", "30") == 0
    assert "pg_replication_lag: 30" in p.read_text(encoding="utf-8")
