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

merged_hash is read from `da-guard effective` (#1549), so every test here
runs with `da_guard_env`; the one that checks the behaviour without da-guard
points `$DA_GUARD_BINARY` at a missing file itself.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("da_guard_env")

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


def test_main_no_args_returns_usage_error(verify_module, conf_d, monkeypatch, capsys, cli_argv):
    """No tenant_id and no --all → exit 1 with error to stderr."""
    monkeypatch.chdir(conf_d.parent)  # cwd has conf.d/
    cli_argv("tenant-verify")
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "tenant_id is required" in captured.err


def test_main_all_with_expect_merged_hash_is_error(verify_module, conf_d, monkeypatch, capsys, cli_argv):
    """--all + --expect-merged-hash makes no sense (one hash, many tenants)."""
    monkeypatch.chdir(conf_d.parent)
    cli_argv("tenant-verify", "--all", "--expect-merged-hash", "deadbeef", "--conf-d", str(conf_d))
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "incompatible with --all" in captured.err


def test_main_conf_d_not_found_returns_error(verify_module, monkeypatch, capsys, tmp_path, cli_argv):
    """Bogus --conf-d path → exit 1."""
    bogus = tmp_path / "nonexistent"
    cli_argv("tenant-verify", "db-fin-a", "--conf-d", str(bogus))
    code = verify_module.main()
    assert code == 1
    captured = capsys.readouterr()
    assert "conf.d not found" in captured.err


def test_main_json_output_is_parseable(verify_module, conf_d, monkeypatch, capsys, cli_argv):
    """--json must emit valid JSON (operator pipes to jq for diff)."""
    import json as _json

    cli_argv("tenant-verify", "db-fin-a", "--conf-d", str(conf_d), "--json")
    code = verify_module.main()
    assert code == 0
    captured = capsys.readouterr()
    parsed = _json.loads(captured.out)
    assert parsed["tenant_id"] == "db-fin-a"
    assert parsed["source_hash"]
    assert parsed["merged_hash"]


def test_main_expect_mismatch_exit_code_2(verify_module, conf_d, monkeypatch, cli_argv):
    """Round-trip the exit-code-2 contract that B-4 checklist depends on."""
    cli_argv("tenant-verify",
            "db-fin-a",
            "--conf-d",
            str(conf_d),
            "--expect-merged-hash",
            "0000000000000000",
            "--json")
    code = verify_module.main()
    assert code == 2


# ── #2093: one tenant declared by two files ─────────────────────────────
#
# The scanner keeps ONE of the two declarations, chosen by filename order.
# Before the fix, rollback checklist item 6 hashed that one: rc 0 `[OK]`
# when the stray file sorted BEFORE the real one, rc 2 MISMATCH when it
# sorted after. Both orders are pinned so neither can pass by accident,
# and the "must ring" controls (single declaration, a clean tenant in the
# same tree, --all on a clean tree) keep rc 0.

_REAL = "tenants:\n  acme:\n    mysql_connections: \"70\"\n"
_STRAY = "tenants:\n  acme:\n    mysql_connections: \"99\"\n"


def _dup_tree(tmp_path, stray_name: str):
    """conf.d with `acme` in real.yaml AND `stray_name`, plus a clean
    tenant `solo` declared once."""
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "real.yaml").write_text(_REAL, encoding="utf-8")
    (root / stray_name).write_text(_STRAY, encoding="utf-8")
    (root / "solo.yaml").write_text(
        "tenants:\n  solo:\n    mysql_connections: \"10\"\n", encoding="utf-8")
    return root


def _pre_base_hash(verify_module, tmp_path):
    """merged_hash of `acme` when only real.yaml declares it."""
    root = tmp_path / "pre"
    root.mkdir()
    (root / "real.yaml").write_text(_REAL, encoding="utf-8")
    info, code = verify_module.verify_one(
        _scanner(verify_module, root), "acme", expect_merged_hash=None)
    assert code == 0
    return info["merged_hash"]


_ORDERS = pytest.mark.parametrize("stray_name, files", [
    ("other.yaml", ["other.yaml", "real.yaml"]),  # stray sorts BEFORE real
    ("zz.yaml", ["real.yaml", "zz.yaml"]),        # stray sorts AFTER real
], ids=["stray-first", "stray-last"])


@_ORDERS
def test_duplicate_declaration_fails_item6_json(
        verify_module, tmp_path, capsys, cli_argv, stray_name, files):
    """Item 6 with the pre-base hash → exit 2 + every file, both orders."""
    import json as _json

    expect = _pre_base_hash(verify_module, tmp_path)
    root = _dup_tree(tmp_path, stray_name)
    cli_argv("tenant-verify", "acme", "--conf-d", str(root),
             "--expect-merged-hash", expect, "--json")
    code = verify_module.main()
    assert code == 2
    parsed = _json.loads(capsys.readouterr().out)
    assert parsed["error"] == "duplicate"
    assert parsed["files"] == files
    assert "merged_hash" not in parsed


@_ORDERS
def test_duplicate_declaration_fails_item6_human(
        verify_module, tmp_path, capsys, cli_argv, stray_name, files):
    """Runbook item 6 runs WITHOUT --json: human output names every file
    and never prints the `[OK]` marker."""
    expect = _pre_base_hash(verify_module, tmp_path)
    root = _dup_tree(tmp_path, stray_name)
    cli_argv("tenant-verify", "acme", "--conf-d", str(root),
             "--expect-merged-hash", expect)
    code = verify_module.main()
    assert code == 2
    out = capsys.readouterr().out
    assert "duplicate" in out
    for f in files:
        assert f"declared in: {f}" in out
    assert "[OK]" not in out


@_ORDERS
def test_duplicate_declaration_fails_without_expect_json(
        verify_module, tmp_path, capsys, cli_argv, stray_name, files):
    """No --expect-merged-hash (the plain lookup the cutover docs teach):
    still exit 2 with every file, not whichever declaration the scan kept."""
    import json as _json

    root = _dup_tree(tmp_path, stray_name)
    cli_argv("tenant-verify", "acme", "--conf-d", str(root), "--json")
    assert verify_module.main() == 2
    parsed = _json.loads(capsys.readouterr().out)
    assert parsed["error"] == "duplicate"
    assert parsed["files"] == files
    assert "merged_hash" not in parsed
    # tenant-verify's own verdict, not describe's "Not describing ..."
    assert "Cannot verify" in parsed["detail"]
    assert "describing" not in parsed["detail"]


@_ORDERS
def test_duplicate_declaration_fails_without_expect_human(
        verify_module, tmp_path, capsys, cli_argv, stray_name, files):
    root = _dup_tree(tmp_path, stray_name)
    cli_argv("tenant-verify", "acme", "--conf-d", str(root))
    assert verify_module.main() == 2
    out = capsys.readouterr().out
    for f in files:
        assert out.count(f"declared in: {f}\n") == 1
    assert "merged_hash:" not in out


def test_duplicate_tree_clean_tenant_has_no_merged_hash(verify_module, tmp_path, capsys,
                                                         cli_argv):
    """#1549: the `duplicate` refusal is still per tenant, but merged_hash
    is da-guard's, and da-guard refuses the whole tree over the duplicate.
    So `solo` is not verified either: exit 1, da-guard's reason on stderr —
    not rc 0 on a hash nothing in Go would serve, and not 2 (a mismatch)."""
    root = _dup_tree(tmp_path, "other.yaml")
    cli_argv("tenant-verify", "solo", "--conf-d", str(root))
    assert verify_module.main() == 1
    captured = capsys.readouterr()
    assert "merged_hash_unavailable" in captured.out, captured.out
    assert 'duplicate tenant ID "acme"' in captured.err, captured.err


def test_single_declaration_item6_passes(verify_module, tmp_path, cli_argv):
    """Must-ring control: the pre-base tree itself → rc 0 against its hash."""
    expect = _pre_base_hash(verify_module, tmp_path)
    cli_argv("tenant-verify", "acme", "--conf-d", str(tmp_path / "pre"),
             "--expect-merged-hash", expect)
    assert verify_module.main() == 0


def test_all_with_duplicate_exits_2_and_keeps_others(
        verify_module, tmp_path, capsys, cli_argv):
    """--all: the duplicated tenant is an error entry (files, no hash),
    the other tenants are still listed, and the run exits 2. They carry no
    merged_hash either: da-guard refuses the tree over the duplicate (#1549)."""
    import json as _json

    root = _dup_tree(tmp_path, "other.yaml")
    cli_argv("tenant-verify", "--all", "--conf-d", str(root), "--json")
    code = verify_module.main()
    assert code == 2
    captured = capsys.readouterr()
    doc = _json.loads(captured.out)
    tenants = {t["tenant_id"]: t for t in doc["tenants"]}
    assert set(tenants) == {"acme", "solo"}
    assert tenants["acme"]["error"] == "duplicate"
    assert tenants["acme"]["files"] == ["other.yaml", "real.yaml"]
    assert "merged_hash" not in tenants["acme"]
    assert tenants["solo"]["error"] == "merged_hash_unavailable"
    # The entry's detail is fixed wording (no file names on the JSON
    # stream, #1607); da-guard's reason is on stderr.
    assert tenants["solo"]["detail"] == verify_module._load_describe_module().MERGED_HASH_DA_GUARD_FAILED
    assert 'duplicate tenant ID "acme"' in captured.err, captured.err


def test_all_human_with_duplicate_exits_2(verify_module, tmp_path, capsys, cli_argv):
    """--all without --json: every file listed, other tenants printed,
    and the refusal summarised on stderr."""
    root = _dup_tree(tmp_path, "zz.yaml")
    cli_argv("tenant-verify", "--all", "--conf-d", str(root))
    assert verify_module.main() == 2
    captured = capsys.readouterr()
    assert "declared in: real.yaml" in captured.out
    assert "declared in: zz.yaml" in captured.out
    assert "tenant_id:     solo" in captured.out
    assert "declared in more than one file" in captured.err
    # Neither is counted as verified: acme is duplicated, and solo has no
    # merged_hash because da-guard refuses the tree (#1549).
    assert "# total: 0 tenants verified, 1 duplicate-declared (not verified)" \
        in captured.out


def test_all_human_total_without_duplicate(verify_module, conf_d, capsys, cli_argv):
    """Must-ring control for the total line: clean tree → all verified."""
    cli_argv("tenant-verify", "--all", "--conf-d", str(conf_d))
    assert verify_module.main() == 0
    assert "# total: 3 tenants verified, 0 duplicate-declared (not verified)" \
        in capsys.readouterr().out


def test_all_without_duplicate_exits_0(verify_module, conf_d, cli_argv):
    """Must-ring control: a clean tree keeps the --all rc 0 contract."""
    cli_argv("tenant-verify", "--all", "--conf-d", str(conf_d), "--json")
    assert verify_module.main() == 0


# ---------------------------------------------------------------------------
# #2459: a selected `_defaults.yaml` that does not parse
# ---------------------------------------------------------------------------

_BROKEN_DEFAULTS = {
    "unclosed-flow": "defaults: [\n",
    "defaults-not-a-mapping": "defaults: [1, 2]\n",
    "tagged-bool-yes": "defaults:\n  x: !!bool yes\n",
    "document-not-a-mapping": "- 1\n- 2\n",
}
# The rest parse, and are refused as an unsupported shape (#2459).
_PARSE_ERRORS = {"unclosed-flow", "tagged-bool-yes"}


@pytest.mark.parametrize("mode", [["db-fin-a"], ["--all"], ["db-fin-a", "--json"]])
@pytest.mark.parametrize("shape", sorted(_BROKEN_DEFAULTS))
@pytest.mark.parametrize("carrier", ["_defaults.yaml", "finance/_defaults.yaml"])
def test_unparseable_defaults_is_a_named_usage_error(
        verify_module, conf_d, capsys, cli_argv, carrier, shape, mode):
    """#2459: exit 1 (this tool's input-error code) naming the file — not
    the `DefaultsParseError` traceback, and not exit 2, which the rollback
    checklist reads as a hash mismatch. Fails (the exception escapes
    `main`) when the catch around `ConfDScanner` is removed."""
    (conf_d / carrier).write_text(_BROKEN_DEFAULTS[shape], encoding="utf-8")
    cli_argv("tenant-verify", *mode, "--conf-d", str(conf_d))
    code = verify_module.main()
    captured = capsys.readouterr()
    assert code == 1, captured.err
    verdict = "does not parse" if shape in _PARSE_ERRORS else "has an unsupported shape"
    assert f"{conf_d / carrier} {verdict}" in captured.err, captured.err
    assert captured.out == ""


def test_control_parseable_defaults_still_verify(verify_module, conf_d, capsys, cli_argv):
    """Must-trigger control for the test above: the same tree with its
    defaults intact verifies with exit 0."""
    cli_argv("tenant-verify", "db-fin-a", "--conf-d", str(conf_d))
    assert verify_module.main() == 0
    assert "does not parse" not in capsys.readouterr().err
