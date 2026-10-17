"""#3116：產生器判為 `domain_policy_unusable` 的 domain policy，所有模式整棵樹拒收。

一條規則：kind 本身就是拒收條件。不論哪個欄位、哪種壞法，產生器的 reader 都在同一個函式
（`_grar_validate.domain_policy_problems`）判 policy 的形狀，記成欄位型別錯誤，
`tree_refusal` 因此在 render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、
`--strict` 都 rc 1、不寫出、不套用。涵蓋：

* policy 本身不是 mapping（null＝沒有約束，照收）；
* `tenants` 不是 list、或有項目不是 tenant id（null＝不套用任何租戶，照收）；
* `constraints` 不是 mapping（null＝沒有約束，照收）；
* list constraint（`forbidden_receiver_types`／`allowed_receiver_types`／`enforce_group_by`）
  不是 list（null＝沒有約束，照收）；`enforce_group_by` 的項目不是非空字串（沿用 group_by
  的 `not_string`／`empty` 規則；過去 mapping 項目讓產生器 TypeError、rc 2）；
* receiver 類型清單有一項不是 `_lib_constants.RECEIVER_TYPES` 的鍵：拼錯的名稱、`opsgenie`、
  非字串（`yes` 是 True、`1`、`~`、mapping）。類型集合與 tenant-config.schema.json 的 receiver
  定義同一組，由 tests/shared/test_receiver_spec_parity.py 釘住，domain-policy.schema.json 的
  enum 也是；
* duration bound（`max_repeat_interval`／`min_group_wait`）解析不了；
* `require_critical_escalation` 不是布林（#2906）；
* 整份 `_domain_policy.yaml` 不能用（語法錯、頂層或 `domain_policies:` 不是 mapping；
  `domain_policies:` 沒有值＝沒有任何 policy，照收）。

在這之前，除了 `require_critical_escalation` 以外都只在 `--strict` 擋：非 strict 時拼錯的
forbidden 類型什麼都不禁止、解析不了的 bound 不生效、整份檔不能用時所有 policy 靜默消失，
rc 都是 0。

租戶 id 都是 fixture 名稱（CLAUDE.md #9）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))
sys.path.insert(0, str(REPO / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib_constants import RECEIVER_TYPES  # noqa: E402
from _grar_parse import load_tenant_tree  # noqa: E402
import explain_route as er  # noqa: E402
import generate_alertmanager_routes as gar  # noqa: E402
import validate_config as vc  # noqa: E402
from test_grar_field_types import (  # noqa: E402
    _MODES,
    _REFUSAL,
    _fake_kubectl,
    _gar,
    _write,
)

_TYPES_TEXT = ", ".join(sorted(RECEIVER_TYPES))


def _doc(body: str) -> dict[str, str]:
    return {"_domain_policy.yaml": body}


def _fin(policy: str) -> dict[str, str]:
    """Domain `fin` whose body is the indented block *policy*."""
    return _doc("domain_policies:\n  fin:\n" + policy)


def _cons(*lines: str) -> dict[str, str]:
    """Domain `fin` over t-a with these constraint lines."""
    return _fin("    tenants: [t-a]\n    constraints:\n"
                + "".join(f"      {line}\n" for line in lines))


# name → (files, finding field, finding policy, text the message holds).
# `policy` None: the file itself is unusable (no domain named).
CASES = {
    "policy-not-mapping": (_doc("domain_policies:\n  fin: [t-a]\n"),
                           None, "fin", "policy must be a mapping, got a list"),
    "policy-scalar": (_doc("domain_policies:\n  fin: 5\n"),
                      None, "fin", "policy must be a mapping, got number 5"),
    "tenants-scalar": (_fin("    tenants: t-a\n    constraints: {max_repeat_interval: 1h}\n"),
                       "tenants", "fin", "'tenants' must be a list, got string 't-a'"),
    "tenants-entry-mapping": (_fin("    tenants: [t-a, {x: 1}]\n"
                                   "    constraints: {max_repeat_interval: 1h}\n"),
                              "tenants", "fin", "'tenants' lists a mapping"),
    "constraints-list": (_fin("    tenants: [t-a]\n    constraints: [max_repeat_interval]\n"),
                         "constraints", "fin", "'constraints' must be a mapping, got a list"),
    "forbidden-not-list": (_cons("forbidden_receiver_types: webhook"),
                           "constraints.forbidden_receiver_types", "fin",
                           "constraint 'forbidden_receiver_types' must be a list, got string 'webhook'"),
    "allowed-not-list": (_cons("allowed_receiver_types: {a: 1}"),
                         "constraints.allowed_receiver_types", "fin",
                         "constraint 'allowed_receiver_types' must be a list, got a mapping"),
    "enforce-group-by-not-list": (_cons("enforce_group_by: tenant"),
                                  "constraints.enforce_group_by", "fin",
                                  "constraint 'enforce_group_by' must be a list"),
    # Entries are label names (non-empty strings), by group_by's element
    # rules; a mapping entry used to crash the run (TypeError, rc 2).
    "enforce-group-by-mapping": (_cons("enforce_group_by: [{a: 1}]"),
                                 "constraints.enforce_group_by", "fin",
                                 "constraint 'enforce_group_by' entry [0] is dict"),
    "enforce-group-by-list": (_cons("enforce_group_by: [tenant, [a]]"),
                              "constraints.enforce_group_by", "fin",
                              "constraint 'enforce_group_by' entry [1] is list"),
    "enforce-group-by-number": (_cons("enforce_group_by: [8]"),
                                "constraints.enforce_group_by", "fin",
                                "constraint 'enforce_group_by' entry [0] is int 8"),
    "enforce-group-by-bool": (_cons("enforce_group_by: [yes]"),
                              "constraints.enforce_group_by", "fin",
                              "constraint 'enforce_group_by' entry [0] is bool True"),
    "enforce-group-by-null-entry": (_cons("enforce_group_by: [tenant, ~]"),
                                    "constraints.enforce_group_by", "fin",
                                    "constraint 'enforce_group_by' entry [1] is NoneType"),
    "enforce-group-by-empty": (_cons("enforce_group_by: ['']"),
                               "constraints.enforce_group_by", "fin",
                               "constraint 'enforce_group_by' entry [0] is an empty string"),
    "max-repeat-banana": (_cons("max_repeat_interval: banana"),
                          "constraints.max_repeat_interval", "fin",
                          "value string 'banana' is not a valid duration"),
    "min-group-wait-negative": (_cons("min_group_wait: -1h"),
                                "constraints.min_group_wait", "fin",
                                "value string '-1h' is not a valid duration"),
    "escalation-quoted": (_cons('require_critical_escalation: "true"'),
                          "constraints.require_critical_escalation", "fin",
                          "must be a boolean (true / false, unquoted), got string 'true'"),
    "file-syntax-error": (_doc("domain_policies: [\n"), None, None,
                          "domain policy file could not be used"),
    "file-top-level-list": (_doc("- fin\n"), None, None,
                            "domain policy file could not be used"),
    "file-domain-policies-list": (_doc("domain_policies: [fin]\n"), "domain_policies", None,
                                  "'domain_policies:' must be a mapping"),
}
# Receiver-type entries: YAML text of one entry → the words the message uses.
_BAD_TYPES = {
    "webhok": "string 'webhok'",       # a misspelt name
    "slak": "string 'slak'",
    "opsgenie": "string 'opsgenie'",   # in the schema enum until #3116
    "yes": "boolean True",             # PyYAML's YAML 1.1 boolean
    "1": "number 1",
    "~": "null",
    "{a: 1}": "a mapping",
}
for _fld, _known in (("forbidden_receiver_types", "slack"), ("allowed_receiver_types", "webhook")):
    for _v, _text in _BAD_TYPES.items():
        # A known type beside the bad entry: the list is refused whole.
        CASES[f"{_fld}={_v}"] = (_cons(f"{_fld}: [{_known}, {_v}]"), f"constraints.{_fld}",
                                 "fin", f"constraint '{_fld}' lists {_text},")


def _errors(d: Path):
    return load_tenant_tree(str(d)).field_type_errors


# ── the reader's verdict ─────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(CASES))
def test_each_problem_is_one_domain_policy_unusable_error(tmp_path, name):
    files, field, policy, text = CASES[name]
    errs = _errors(_write(tmp_path, files))
    assert [(e.kind, e.field, e.policy) for e in errs] == [
        ("domain_policy_unusable", field, policy)], errs
    assert text in errs[0].message, errs[0].message


def test_a_receiver_type_message_lists_the_types(tmp_path):
    [err] = _errors(_write(tmp_path, _cons("forbidden_receiver_types: [webhok, slack, yes]")))
    assert ("lists string 'webhok', boolean True, which are not receiver types"
            in err.message), err.message
    assert err.message.endswith(f"the receiver types are {_TYPES_TEXT}"), err.message


def test_every_problem_of_a_policy_is_reported(tmp_path):
    errs = _errors(_write(tmp_path, _fin(
        "    tenants: t-a\n    constraints:\n"
        "      forbidden_receiver_types: [webhok]\n      allowed_receiver_types: slack\n"
        "      max_repeat_interval: banana\n      require_critical_escalation: 1\n")))
    assert sorted(e.field for e in errs) == [
        "constraints.allowed_receiver_types", "constraints.forbidden_receiver_types",
        "constraints.max_repeat_interval", "constraints.require_critical_escalation",
        "tenants"], errs


def test_a_subtree_policy_is_judged_too(tmp_path):
    d = _write(tmp_path, {})
    (d / "team").mkdir()
    (d / "t-a.yaml").rename(d / "team" / "t-a.yaml")
    (d / "team" / "_domain_policy.yaml").write_text(
        _cons("max_repeat_interval: banana")["_domain_policy.yaml"], encoding="utf-8")
    assert [(e.kind, e.field) for e in _errors(d)] == [
        ("domain_policy_unusable", "constraints.max_repeat_interval")]


# Null is "not set" wherever the schema allows it; legal values render.
_LEGAL = {
    "policy-null": _doc("domain_policies:\n  fin: ~\n"),
    # #3116: null is "no tenant" / "no policy", as the schema allows — both
    # used to be refused as "not a list" / "not a mapping".
    "tenants-null": _fin("    tenants: ~\n    constraints: {forbidden_receiver_types: [webhook]}\n"),
    "domain-policies-null": _doc("domain_policies:\n"),
    "domain-policies-tilde": _doc("domain_policies: ~\n"),
    "enforce-group-by-repeat-and-wildcard": _cons("enforce_group_by: [tenant, tenant, '...']"),
    "constraints-null": _fin("    tenants: [t-a]\n    constraints: ~\n"),
    "constraints-absent": _fin("    tenants: [t-a]\n"),
    "tenants-absent": _fin("    constraints: {max_repeat_interval: 1h}\n"),
    "lists-null": _cons("forbidden_receiver_types: ~", "allowed_receiver_types: ~",
                        "enforce_group_by: ~"),
    "lists-empty": _cons("forbidden_receiver_types: []", "allowed_receiver_types: []"),
    "every-type": _cons("allowed_receiver_types: [" + ", ".join(sorted(RECEIVER_TYPES)) + "]"),
    "bounds": _cons("max_repeat_interval: 1h30m", "min_group_wait: 0s"),
    "bounds-null": _cons("max_repeat_interval: ~", "min_group_wait: ~",
                         "require_critical_escalation: ~"),
    "empty-file": _doc(""),
}


@pytest.mark.parametrize("name", sorted(_LEGAL))
def test_a_policy_written_correctly_is_no_error_and_renders(tmp_path, name):
    d = _write(tmp_path, _LEGAL[name])
    assert _errors(d) == []
    p = _gar(d, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert _REFUSAL not in p.stderr


@pytest.mark.parametrize("constraint,rc_text", [
    ("forbidden_receiver_types: [webhook]", "receiver type 'webhook' is forbidden"),
    ("enforce_group_by: [team]", "missing required labels: ['team']"),
])
def test_a_legal_constraint_is_still_enforced(tmp_path, constraint, rc_text):
    """The tenants send to webhook and set no group_by: a correctly written
    constraint they break is a violation, blocking under --strict — judged,
    not just accepted."""
    d = _write(tmp_path, _cons(constraint))
    p = _gar(d, "--validate", "--strict")
    assert p.returncode == 1, p.stderr
    assert rc_text in p.stderr, p.stderr
    assert _REFUSAL not in p.stderr


# ── every mode refuses, nothing is written or applied ────────────────────

@pytest.mark.parametrize("name", sorted(CASES))
def test_every_mode_refuses_and_writes_nothing(tmp_path, name):
    d = _write(tmp_path, CASES[name][0])
    out = tmp_path / "out.yaml"
    bindir, calls = _fake_kubectl(tmp_path)
    for mode, argv in _MODES.items():
        p = _gar(d, *argv(out), path=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
        assert p.returncode == 1, (mode, p.returncode, p.stderr)
        assert _REFUSAL in p.stderr, (mode, p.stderr)
        assert "ERROR (field type):" in p.stderr, (mode, p.stderr)
        assert not out.exists(), f"{mode} wrote {out}"
        assert "tenant-t-b" not in p.stdout, (mode, p.stdout)
        assert "OK:" not in p.stdout, (mode, p.stdout)
    assert not calls.exists(), f"--apply ran kubectl: {calls.read_text(encoding='utf-8')}"


@pytest.mark.parametrize("name", ["tenants-scalar", "max-repeat-banana", "file-syntax-error",
                                  "forbidden_receiver_types=opsgenie"])
def test_the_findings_document_names_the_problem(tmp_path, name):
    files, field, policy, _text = CASES[name]
    d = _write(tmp_path, files)
    doc_path = tmp_path / "findings.json"
    p = _gar(d, "--dry-run", "--findings-json", str(doc_path))
    assert p.returncode == 1, p.stderr
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    items = [r for r in doc["findings"] if r["kind"] != "refusal_summary"]
    assert [(r["kind"], r["field"], r["severity"], r["blocks"]) for r in items] == [
        ("domain_policy_unusable", field, "error", "always")], items


def test_no_policy_problem_is_left_to_strict(tmp_path):
    """No `domain_policy_unusable` finding is blocks="strict" any more: the
    kind refuses in every mode, never only under --strict."""
    for name, (files, *_rest) in CASES.items():
        d = _write(tmp_path / name, files)
        tree = load_tenant_tree(str(d), strict_policies=True)
        loose = [f for f in tree.schema_warnings
                 if getattr(f, "kind", None) == "domain_policy_unusable"
                 and getattr(f, "blocks", None) != "always"]
        assert not loose, (name, loose)


# ── the consumers that refuse through tree_refusal ───────────────────────

@pytest.mark.parametrize("name", ["policy-not-mapping", "constraints-list", "file-syntax-error",
                                  "allowed_receiver_types=slak"])
def test_tree_refusal_refuses_it_for_every_consumer(tmp_path, name):
    """explain-route --trace, notification-tester and policy-engine refuse
    through `tree_refusal` with the tree's `field_type_errors`."""
    tree = load_tenant_tree(str(_write(tmp_path, CASES[name][0])))
    rc, lines = gar.tree_refusal(tree.files_read, tree.tenant_file_errors,
                                 tree.duplicate_tenants, tree.routing_tree_problems,
                                 tree.invalid_tenant_ids, tree.field_type_errors)
    assert rc == 1 and _REFUSAL in lines[0], lines


@pytest.mark.parametrize("name", ["tenants-scalar", "max-repeat-banana", "file-syntax-error"])
def test_validate_config_schema_row_fails(tmp_path, name):
    row = vc.check_schema(str(_write(tmp_path, CASES[name][0])))
    assert row["status"] == vc.FAIL, row
    assert any("ERROR (field type):" in m for m in row["details"]), row


@pytest.mark.parametrize("name", ["forbidden_receiver_types=webhok",
                                  "forbidden_receiver_types={a: 1}", "constraints-list"])
def test_trace_reads_no_policy_of_a_refused_tree(tmp_path, capsys, name):
    """explain-route --trace says the generator refuses the tree, and its
    policy step reads no policy then (it used to read the list leniently)."""
    d = _write(tmp_path, CASES[name][0])
    rc = er.main(["--config-dir", str(d), "--tenant", "t-a", "--trace", "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "the generator refuses this config" in captured.err, captured.err
    [trace] = json.loads(captured.out)
    step5 = trace["steps"][4]
    assert step5["action"] == "policy_check"
    assert step5["passed"] is None
    assert "generator refuses this config" in step5["detail"], step5
