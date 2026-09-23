"""#1652: no validate_config row may PASS on a tree it did not read.

Several rows get their tenants from a reader that is FLAT (top level of
``--config-dir`` only) while threshold-exporter reads the tree recursively.
Measured before the fix: the same broken tenant file at ``db-a.yaml`` gave
``[FAIL] routes`` / exit 1, and at ``prod/db-a.yaml`` gave
``[PASS] routes  0 routes, 0 receivers`` / ``Result: PASS`` / exit 0.

⛔ The property is DERIVED, not enumerated. Nothing below names which rows
are flat: every row name comes from the tool's own ``--json`` output, and the
assertion is the same for all of them — "if the flat twin of a tree is not
PASS on this row, the hierarchical twin is not PASS either". A recursive row
satisfies it by finding the same problem; a flat row can only satisfy it by
admitting what it skipped. A row that starts reading flat later is covered
without anyone updating a list, and a row added later with no corpus entry
that makes it fire reddens ``test_the_corpus_exercises_every_row``.
"""

from __future__ import annotations

import json
import pathlib

import pytest

import validate_config as vc


_DEFAULTS = "defaults:\n  mysql_connections: 80\n"
_GOOD_TENANT = (
    "tenants:\n"
    "  db-a:\n"
    "    mysql_connections: \"70\"\n"
    "    _routing:\n"
    "      receiver:\n"
    "        type: webhook\n"
    "        url: https://allowed.example.com/hook\n"
)
_POLICY = "allowed_domains: [\"allowed.example.com\"]\n"

# Each entry: (id, top-level files, tenant files, extra argv). Tenant files
# are what the hierarchical twin moves into a subdirectory; top-level files
# (`_defaults.yaml`, `_profiles.yaml`, ...) stay where they are in both.
# Every entry is meant to make at least one row non-PASS on the FLAT twin —
# `test_flat_twin_fires` checks that it does.
_CORPUS = [
    ("unknown-key", {}, {
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_connectionz: \"70\"\n"},
     []),
    ("receiver-not-an-object", {}, {
        "db-a.yaml": ("tenants:\n  db-a:\n    mysql_connections: \"70\"\n"
                      "    _routing:\n      receiver: \"not-an-object\"\n")},
     []),
    ("webhook-outside-allowlist", {}, {
        "db-a.yaml": ("tenants:\n  db-a:\n    mysql_connections: \"70\"\n"
                      "    _routing:\n      receiver:\n"
                      "        type: webhook\n"
                      "        url: https://hooks.bad.com/x\n")},
     ["--policy", "{policy}"]),
    ("dsl-policy-violated", {
        "_defaults.yaml": (_DEFAULTS +
                           "_policies:\n"
                           "  - name: routing-required\n"
                           "    description: Routing required\n"
                           "    target: _routing\n"
                           "    operator: required\n"
                           "    severity: error\n")}, {
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: \"70\"\n"},
     []),
    ("unknown-profile", {}, {
        "db-a.yaml": ("tenants:\n  db-a:\n    mysql_connections: \"70\"\n"
                      "    _profile: no-such-profile\n")},
     []),
    ("tenant-declared-twice", {}, {
        "db-a.yaml": "tenants:\n  db-a:\n    mysql_connections: \"70\"\n",
        "db-a-copy.yaml": "tenants:\n  db-a:\n    mysql_connections: \"60\"\n"},
     []),
    ("top-level-is-a-list", {}, {
        "db-a.yaml": "- not\n- a\n- mapping\n"},
     []),
]


def _tree(root: pathlib.Path, top: dict[str, str], tenants: dict[str, str],
          sub: str | None) -> str:
    d = root / "conf.d"
    d.mkdir(parents=True)
    files = {"_defaults.yaml": _DEFAULTS, **top}
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    where = d / sub if sub else d
    where.mkdir(exist_ok=True)
    for name, body in tenants.items():
        (where / name).write_text(body, encoding="utf-8")
    return str(d)


def _run(config_dir: str, extra: list[str], tmp_path: pathlib.Path,
         cli_argv, capsys) -> tuple[int, dict[str, dict]]:
    policy = tmp_path / "policy.yaml"
    policy.write_text(_POLICY, encoding="utf-8")
    argv = [a.replace("{policy}", str(policy)) for a in extra]
    cli_argv("validate_config", "--config-dir", config_dir, "--json", *argv)
    with pytest.raises(SystemExit) as exc:
        vc.main()
    rows = json.loads(capsys.readouterr().out)
    return exc.value.code, {r["check"]: r for r in rows}


def _twins(case, tmp_path, cli_argv, capsys):
    _id, top, tenants, extra = case
    flat = _run(_tree(tmp_path / "flat", top, tenants, None), extra,
                tmp_path, cli_argv, capsys)
    hier = _run(_tree(tmp_path / "hier", top, tenants, "prod"), extra,
                tmp_path, cli_argv, capsys)
    return flat, hier


@pytest.mark.parametrize("case", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_flat_twin_fires(case, tmp_path, cli_argv, capsys):
    """Must-fire control: otherwise the property below is vacuous."""
    (_rc, flat), _hier = _twins(case, tmp_path, cli_argv, capsys)
    fired = [n for n, r in flat.items() if r["status"] != vc.PASS]
    assert fired, f"{case[0]}: flat twin is all-PASS: {flat}"


@pytest.mark.parametrize("case", _CORPUS, ids=[c[0] for c in _CORPUS])
def test_hier_twin_is_never_pass_where_flat_twin_is_not(
        case, tmp_path, cli_argv, capsys):
    (_frc, flat), (_hrc, hier) = _twins(case, tmp_path, cli_argv, capsys)
    # Same row set on both twins — a row that vanished on the hierarchical
    # tree would dodge the comparison below.
    assert sorted(flat) == sorted(hier), (sorted(flat), sorted(hier))
    wrong = {n: hier[n] for n in flat
             if flat[n]["status"] != vc.PASS and hier[n]["status"] == vc.PASS}
    assert not wrong, (
        f"{case[0]}: these rows find a problem in the flat tree and report "
        f"PASS on the identical tree with the tenant file in prod/: {wrong}")


def test_the_corpus_exercises_every_row(tmp_path, cli_argv, capsys):
    """Every row the tool emits for these argv shapes must be made to fire by
    SOME corpus entry, or the property above says nothing about it.

    Row names come from the output. A row added to the tool reddens this
    until a corpus entry makes it non-PASS on a flat tree.
    """
    seen: set[str] = set()
    fired: set[str] = set()
    for i, case in enumerate(_CORPUS):
        _id, top, tenants, extra = case
        _rc, rows = _run(_tree(tmp_path / f"c{i}", top, tenants, None),
                         extra, tmp_path, cli_argv, capsys)
        seen |= set(rows)
        fired |= {n for n, r in rows.items() if r["status"] != vc.PASS}
    assert seen, "the tool emitted no rows at all"
    assert seen <= fired, f"rows never made to fire: {sorted(seen - fired)}"


def test_the_skipped_file_is_named_in_json(tmp_path, cli_argv, capsys):
    """The skipped path reaches --json, not only stderr."""
    case = _CORPUS[1]  # receiver-not-an-object
    _flat, (rc, hier) = _twins(case, tmp_path, cli_argv, capsys)
    flagged = {n: r for n, r in hier.items() if r.get("skipped_nested_files")}
    assert flagged, hier
    for n, r in flagged.items():
        assert r["skipped_nested_files"] == ["prod/db-a.yaml"], (n, r)
        assert any("prod/db-a.yaml" in d for d in r["details"]), (n, r)
        assert r["status"] != vc.PASS, (n, r)
    # ⚠️ Documented contract: WARN is exit 0; the signal is in Result/--json.
    assert rc == 0, rc


def test_a_valid_flat_tree_stays_all_pass(tmp_path, cli_argv, capsys):
    """Negative control: no spurious WARN on a tree every reader can see."""
    d = _tree(tmp_path, {}, {"db-a.yaml": _GOOD_TENANT}, None)
    rc, rows = _run(d, ["--policy", "{policy}"], tmp_path, cli_argv, capsys)
    not_pass = {n: r for n, r in rows.items() if r["status"] != vc.PASS}
    assert not not_pass, not_pass
    assert not any(r.get("skipped_nested_files") for r in rows.values()), rows
    assert rc == 0, rc


def test_a_row_that_consulted_no_reader_keeps_its_pass(tmp_path):
    """The exception is structural, not a list: a check that returns before
    reaching a tenant reader was never told anything by one.

    ``check_policy`` with a policy holding no allowlist and
    ``check_policy_dsl`` with no policies both return before loading tenants;
    on a hierarchical tree they must stay PASS, while ``check_routes`` on the
    same tree (which does load tenants flat) must not.
    """
    d = _tree(tmp_path, {}, {"db-a.yaml": _GOOD_TENANT}, "prod")
    empty_policy = tmp_path / "empty-policy.yaml"
    empty_policy.write_text("allowed_domains: []\n", encoding="utf-8")
    policy = vc._run_check("policy", vc.check_policy, d, str(empty_policy),
                           _config_dir=d)
    dsl = vc._run_check("policy_dsl", vc.check_policy_dsl, d, None,
                        _config_dir=d)
    routes = vc._run_check("routes", vc.check_routes, d, None, _config_dir=d)
    assert policy["status"] == vc.PASS, policy
    assert dsl["status"] == vc.PASS, dsl
    assert routes["status"] == vc.WARN, routes
    assert routes["skipped_nested_files"] == ["prod/db-a.yaml"], routes
