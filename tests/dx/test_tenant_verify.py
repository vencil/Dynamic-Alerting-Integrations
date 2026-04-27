"""Tests for scripts/tools/dx/tenant_verify.py.

Use case context: B-4 Emergency Rollback Procedures verification
checklist (item 6 in `docs/scenarios/incremental-migration-playbook.md`
§Emergency Rollback Procedures). After a batch-PR rollback wave, the
operator runs `da-tools tenant-verify <id> --expect-merged-hash <h>` for
each affected tenant and expects exit code 0 if the merged_hash returned
to the pre-Base-PR snapshot, exit code 2 otherwise.

Tests focus on:
  - happy path single tenant (exit 0, info dict shape)
  - --expect-merged-hash match (exit 0)
  - --expect-merged-hash mismatch (exit 2)
  - tenant not found (exit 2)
  - --all path
  - argument error paths (no tenant_id without --all, --all + --expect-*)
  - JSON output shape
  - inheritance chain reflected in output

Per S#32 lesson: assertions on hashes are equality on string values
(not invariant ranges), because canonical_hash is deterministic by
construction.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TOOL_PATH = REPO_ROOT / "scripts" / "tools" / "dx" / "tenant_verify.py"


@pytest.fixture(scope="module")
def verify_module():
    """Load tenant_verify.py as a module."""
    spec = importlib.util.spec_from_file_location("tenant_verify", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tenant_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def conf_d(tmp_path):
    """Build a minimal conf.d hierarchy with cascading defaults + 3 tenants.

    Layout:
        conf.d/
          _defaults.yaml          (root — sets mysql_connections=80)
          finance/
            _defaults.yaml        (region — adds redis_pool=50)
            db-fin-a.yaml         (tenant — overrides mysql_connections=200)
          marketing/
            db-mkt-a.yaml         (tenant — no override)
            db-mkt-b.yaml         (tenant — overrides redis_pool=99)
    """
    root = tmp_path / "conf.d"
    root.mkdir()

    (root / "_defaults.yaml").write_text(
        "defaults:\n  mysql_connections: 80\n", encoding="utf-8"
    )

    finance = root / "finance"
    finance.mkdir()
    (finance / "_defaults.yaml").write_text(
        "defaults:\n  redis_pool: 50\n", encoding="utf-8"
    )
    (finance / "db-fin-a.yaml").write_text(
        "tenants:\n  db-fin-a:\n    mysql_connections: 200\n",
        encoding="utf-8",
    )

    marketing = root / "marketing"
    marketing.mkdir()
    (marketing / "db-mkt-a.yaml").write_text(
        "tenants:\n  db-mkt-a:\n    redis_pool: 50\n",
        encoding="utf-8",
    )
    (marketing / "db-mkt-b.yaml").write_text(
        "tenants:\n  db-mkt-b:\n    redis_pool: 99\n",
        encoding="utf-8",
    )

    return root


def _scanner(verify_module, conf_d):
    describe = verify_module._load_describe_module()
    return describe.ConfDScanner(conf_d)


def test_verify_one_happy_path(verify_module, conf_d):
    scanner = _scanner(verify_module, conf_d)
    info, code = verify_module.verify_one(scanner, "db-fin-a", expect_merged_hash=None)
    assert code == 0
    assert info["tenant_id"] == "db-fin-a"
    assert info["source_hash"]  # non-empty
    assert info["merged_hash"]  # non-empty
    assert info["expected_merged_hash"] is None
    assert info["match"] is None
    # source_file path is conf.d-relative
    assert "db-fin-a.yaml" in info["source_file"]


def test_verify_one_expect_merged_hash_match(verify_module, conf_d):
    scanner = _scanner(verify_module, conf_d)
    # Round-trip: get the actual hash, then pass it as expected.
    truth, _ = verify_module.verify_one(scanner, "db-fin-a", expect_merged_hash=None)
    info, code = verify_module.verify_one(
        scanner, "db-fin-a", expect_merged_hash=truth["merged_hash"]
    )
    assert code == 0
    assert info["match"] is True
    assert info["expected_merged_hash"] == truth["merged_hash"]


def test_verify_one_expect_merged_hash_mismatch(verify_module, conf_d):
    scanner = _scanner(verify_module, conf_d)
    info, code = verify_module.verify_one(
        scanner, "db-fin-a", expect_merged_hash="0000000000000000"
    )
    assert code == 2  # B-4 checklist signal
    assert info["match"] is False
    assert info["expected_merged_hash"] == "0000000000000000"


def test_verify_one_tenant_not_found(verify_module, conf_d):
    scanner = _scanner(verify_module, conf_d)
    info, code = verify_module.verify_one(scanner, "ghost-tenant", expect_merged_hash=None)
    assert code == 2
    assert info["error"] == "not_found"
    assert info["tenant_id"] == "ghost-tenant"


def test_verify_all_returns_all_tenants(verify_module, conf_d):
    scanner = _scanner(verify_module, conf_d)
    results = verify_module.verify_all(scanner)
    tids = [r["tenant_id"] for r in results]
    assert sorted(tids) == ["db-fin-a", "db-mkt-a", "db-mkt-b"]
    # All three should have non-empty merged_hash
    for r in results:
        assert r["merged_hash"], f"tenant {r['tenant_id']} missing merged_hash"


def test_verify_all_results_are_sorted(verify_module, conf_d):
    """Stable ordering — operator running --all on rollback wave needs
    deterministic output to diff against pre-base snapshot."""
    scanner = _scanner(verify_module, conf_d)
    results = verify_module.verify_all(scanner)
    tids = [r["tenant_id"] for r in results]
    assert tids == sorted(tids)


def test_inheritance_chain_reflected_in_output(verify_module, conf_d):
    """db-fin-a should show 2-level chain (root + finance), db-mkt-a
    should show 1-level (root only)."""
    scanner = _scanner(verify_module, conf_d)
    fin, _ = verify_module.verify_one(scanner, "db-fin-a", expect_merged_hash=None)
    mkt, _ = verify_module.verify_one(scanner, "db-mkt-a", expect_merged_hash=None)

    # Counts (paths use OS separator, so compare lengths not contents).
    assert len(fin["defaults_chain"]) == 2, f"finance tenant: {fin['defaults_chain']}"
    assert len(mkt["defaults_chain"]) == 1, f"marketing tenant: {mkt['defaults_chain']}"


def test_main_no_args_returns_usage_error(verify_module, conf_d, monkeypatch, capsys):
    """No tenant_id and no --all → exit 1 with error to stderr."""
    monkeypatch.chdir(conf_d.parent)  # cwd has conf.d/
    monkeypatch.setattr(sys, "argv", ["tenant-verify"])
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "tenant_id is required" in captured.err


def test_main_all_with_expect_merged_hash_is_error(verify_module, conf_d, monkeypatch, capsys):
    """--all + --expect-merged-hash makes no sense (one hash, many tenants)."""
    monkeypatch.chdir(conf_d.parent)
    monkeypatch.setattr(
        sys,
        "argv",
        ["tenant-verify", "--all", "--expect-merged-hash", "deadbeef", "--conf-d", str(conf_d)],
    )
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "incompatible with --all" in captured.err


def test_main_conf_d_not_found_returns_error(verify_module, monkeypatch, capsys, tmp_path):
    """Bogus --conf-d path → exit 1."""
    bogus = tmp_path / "nonexistent"
    monkeypatch.setattr(
        sys,
        "argv",
        ["tenant-verify", "db-fin-a", "--conf-d", str(bogus)],
    )
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "conf.d not found" in captured.err


def test_main_json_output_is_parseable(verify_module, conf_d, monkeypatch, capsys):
    """--json must emit valid JSON (operator pipes to jq for diff)."""
    import json as _json

    monkeypatch.setattr(
        sys,
        "argv",
        ["tenant-verify", "db-fin-a", "--conf-d", str(conf_d), "--json"],
    )
    code = verify_module.main()
    assert code == 0
    captured = capsys.readouterr()
    parsed = _json.loads(captured.out)
    assert parsed["tenant_id"] == "db-fin-a"
    assert parsed["source_hash"]
    assert parsed["merged_hash"]


def test_main_expect_mismatch_exit_code_2(verify_module, conf_d, monkeypatch):
    """Round-trip the exit-code-2 contract that B-4 checklist depends on."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tenant-verify",
            "db-fin-a",
            "--conf-d",
            str(conf_d),
            "--expect-merged-hash",
            "0000000000000000",
            "--json",
        ],
    )
    code = verify_module.main()
    assert code == 2
