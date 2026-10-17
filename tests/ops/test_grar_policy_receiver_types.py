"""#3116：domain policy 的 receiver 類型清單只收產生器做得出來的類型，否則整棵樹拒收。

`constraints.forbidden_receiver_types`／`allowed_receiver_types` 的每一項必須是字串，且是
`_lib_constants.RECEIVER_TYPES` 的鍵（與 tenant-config.schema.json 的 receiver 定義同一組，
由 tests/shared/test_receiver_spec_parity.py 釘住，domain-policy.schema.json 的 enum 也是）。

在這之前產生器只檢查項目可否 hash：

* `forbidden_receiver_types: [webhok]` 什麼都不禁止，rc 0（fail-open）；
* `allowed_receiver_types: [slak]` 讓每個 receiver 都違規，訊息卻不說是清單本身拼錯；
* `opsgenie` 在 schema enum 裡，產生器卻根本做不出 opsgenie receiver。

現在未知名稱、非字串（`yes` 是 True、`1`、`~` 項、mapping 項）一律是 `domain_policy_unusable`
的欄位型別錯誤，比照 #2906 的 `require_critical_escalation`：所有模式 rc 1、不寫出、不套用，
走 `tree_refusal` 那一條路。清單本身是 null 或不設＝不約束，照舊。

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
from test_grar_field_types import (  # noqa: E402
    _MODES,
    _REFUSAL,
    _fake_kubectl,
    _gar,
    _write,
)

_FIELDS = ("forbidden_receiver_types", "allowed_receiver_types")
# YAML text of one list entry → the words the message names it by.
_BAD = {
    "webhok": "string 'webhok'",       # a misspelt name
    "slak": "string 'slak'",
    "opsgenie": "string 'opsgenie'",   # in the schema enum until #3116
    "yes": "boolean True",             # PyYAML's YAML 1.1 boolean
    "1": "number 1",
    "~": "null",
    "{a: 1}": "a mapping",
}
_CASES = [(f, v) for f in _FIELDS for v in _BAD]
_TYPES_TEXT = ", ".join(sorted(RECEIVER_TYPES))


def _policy(field: str, items: str, *, level: str = "") -> dict[str, str]:
    """A policy over t-a whose *field* is the YAML flow list *items*."""
    return {f"{level}_domain_policy.yaml": (
        "domain_policies:\n  fin:\n    tenants: [t-a]\n    constraints:\n"
        f"      {field}: {items}\n")}


def _bad_list(field: str, value: str) -> str:
    # A known type beside the bad entry: the list is refused whole, not
    # judged on the entries that are fine.
    return f"[{'slack' if field.startswith('forbidden') else 'webhook'}, {value}]"


# ── the reader's verdict ─────────────────────────────────────────────────

@pytest.mark.parametrize("field,value", _CASES, ids=[f"{f}={v}" for f, v in _CASES])
def test_an_entry_that_is_no_receiver_type_is_one_field_type_error(tmp_path, field, value):
    errs = load_tenant_tree(str(_write(tmp_path, _policy(field, _bad_list(field, value))))
                            ).field_type_errors
    assert [(e.kind, e.field, e.policy) for e in errs] == [
        ("domain_policy_unusable", f"constraints.{field}", "fin")], errs
    msg = errs[0].message
    assert f"constraint '{field}' lists {_BAD[value]}," in msg, msg
    assert msg.endswith(f"the receiver types are {_TYPES_TEXT}"), msg


def test_every_bad_entry_of_a_list_is_named(tmp_path):
    errs = load_tenant_tree(str(_write(tmp_path, _policy(
        "forbidden_receiver_types", "[webhok, slack, yes]")))).field_type_errors
    assert len(errs) == 1
    assert "lists string 'webhok', boolean True, which are not receiver types" in errs[0].message


def test_both_lists_are_judged(tmp_path):
    files = {"_domain_policy.yaml": (
        "domain_policies:\n  fin:\n    tenants: [t-a]\n    constraints:\n"
        "      forbidden_receiver_types: [webhok]\n"
        "      allowed_receiver_types: [slak]\n")}
    errs = load_tenant_tree(str(_write(tmp_path, files))).field_type_errors
    assert sorted(e.field for e in errs) == [
        "constraints.allowed_receiver_types", "constraints.forbidden_receiver_types"]


def test_a_subtree_policy_is_judged_too(tmp_path):
    d = _write(tmp_path, {})
    (d / "team").mkdir()
    (d / "t-a.yaml").rename(d / "team" / "t-a.yaml")
    (d / "team" / "_domain_policy.yaml").write_text(
        _policy("forbidden_receiver_types", "[webhok]")["_domain_policy.yaml"],
        encoding="utf-8")
    errs = load_tenant_tree(str(d)).field_type_errors
    assert [(e.kind, e.field) for e in errs] == [
        ("domain_policy_unusable", "constraints.forbidden_receiver_types")], errs


_LEGAL = ["[" + ", ".join(sorted(RECEIVER_TYPES)) + "]", "[]", "~", "[email]"]


@pytest.mark.parametrize("field", _FIELDS)
@pytest.mark.parametrize("items", _LEGAL)
def test_a_list_of_receiver_types_is_no_error_and_renders(tmp_path, field, items):
    d = _write(tmp_path, _policy(field, items))
    assert load_tenant_tree(str(d)).field_type_errors == []
    p = _gar(d, "--dry-run")
    assert p.returncode == 0, p.stderr
    assert _REFUSAL not in p.stderr


def test_a_legal_forbidden_type_is_still_enforced(tmp_path):
    """The tenants send to webhook: forbidding it is a violation, blocking
    under --strict — the constraint is judged, not just accepted."""
    d = _write(tmp_path, _policy("forbidden_receiver_types", "[webhook]"))
    p = _gar(d, "--validate", "--strict")
    assert p.returncode == 1, p.stderr
    assert "receiver type 'webhook' is forbidden" in p.stderr, p.stderr
    assert _REFUSAL not in p.stderr


# ── every mode refuses, nothing is written or applied ────────────────────

@pytest.mark.parametrize("field,value", _CASES, ids=[f"{f}={v}" for f, v in _CASES])
def test_every_mode_refuses_and_writes_nothing(tmp_path, field, value):
    d = _write(tmp_path, _policy(field, _bad_list(field, value)))
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


@pytest.mark.parametrize("field", _FIELDS)
def test_the_findings_document_names_the_field(tmp_path, field):
    d = _write(tmp_path, _policy(field, "[opsgenie]"))
    doc_path = tmp_path / "findings.json"
    p = _gar(d, "--dry-run", "--findings-json", str(doc_path))
    assert p.returncode == 1, p.stderr
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    items = [r for r in doc["findings"] if r["kind"] != "refusal_summary"]
    assert [(r["kind"], r["field"], r["severity"], r["blocks"]) for r in items] == [
        ("domain_policy_unusable", f"constraints.{field}", "error", "always")], items


# ── the consumers that refuse through tree_refusal ───────────────────────

@pytest.mark.parametrize("field", _FIELDS)
def test_tree_refusal_refuses_it_for_every_consumer(tmp_path, field):
    """explain-route --trace, notification-tester and policy-engine refuse
    through `tree_refusal` with the tree's `field_type_errors`."""
    tree = load_tenant_tree(str(_write(tmp_path, _policy(field, "[webhok]"))))
    rc, lines = gar.tree_refusal(tree.files_read, tree.tenant_file_errors,
                                 tree.duplicate_tenants, tree.routing_tree_problems,
                                 tree.invalid_tenant_ids, tree.field_type_errors)
    assert rc == 1 and _REFUSAL in lines[0], lines
    assert any("webhok" in line for line in lines), lines


@pytest.mark.parametrize("value", ["webhok", "{a: 1}"])
def test_trace_reads_no_policy_of_a_refused_tree(tmp_path, capsys, value):
    """explain-route --trace says the generator refuses the tree, and its
    policy step reads no policy then (it used to read the list leniently)."""
    d = _write(tmp_path, _policy("forbidden_receiver_types", f"[{value}]"))
    rc = er.main(["--config-dir", str(d), "--tenant", "t-a", "--trace", "--json"])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "the generator refuses this config" in captured.err, captured.err
    [trace] = json.loads(captured.out)
    step5 = trace["steps"][4]
    assert step5["action"] == "policy_check"
    assert step5["passed"] is None
    assert "generator refuses this config" in step5["detail"], step5
