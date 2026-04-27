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
