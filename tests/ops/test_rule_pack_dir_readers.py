"""The ops tools that read a rule-packs directory take its rule packs from the
one declaration, `_lib_consumer_read.rule_pack_files` (#3426).

Each of `operator-generate`, `operator-check`, `runtime-audit` and
`rule-pack-split` used to `glob("rule-pack-*.yaml")` on its own: a `.yml`
pack was not seen, and a YAML beside the packs that is neither a pack nor
declared there was passed over in silence. Each case below turns red if its
tool goes back to a glob of its own.
"""
from __future__ import annotations

import argparse
from types import SimpleNamespace

import pytest

import _lib_consumer_read as cr
import generate_rule_pack_split as grps
import operator_check as oc
import operator_generate as og
import runtime_audit as ra

_PACK = ("groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up{tenant=\"t\"} == 0\n"
         "    labels: {severity: warning}\n")


def _rule_packs(tmp_path, *, extra: bool):
    rp = tmp_path / "rule-packs"
    rp.mkdir()
    (rp / "rule-pack-x.yml").write_text(_PACK, encoding="utf-8")
    (rp / "threshold-registry.yaml").write_text("entries: []\n", encoding="utf-8")
    if extra:
        (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    return rp


def test_operator_generate(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    assert [p.name for p in og.discover_rule_packs(rp)] == ["rule-pack-x.yml"]
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    with pytest.raises(cr.SourcesUnlisted, match="cannot be chosen"):
        og.discover_rule_packs(rp)


def _rules_check(rp):
    checker = oc.OperatorChecker(SimpleNamespace(
        namespace="monitoring", rule_packs_dir=str(rp), prometheus=None,
        kubeconfig=None, json=False, ci=False))
    checker._installed = lambda crd: False
    return checker.check_prometheus_rule_status()


def test_operator_check(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    assert _rules_check(rp).detail.startswith("0/1")   # the .yml pack is expected
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    result = _rules_check(rp)
    assert result.status == "fail" and result.caller_error and "extra.yaml" in result.detail


def _audit_args(rp):
    return argparse.Namespace(rule_packs_dir=str(rp), prometheus=None, runtime_json=None,
                              strict_orphan=False, json=False, ci=False)


def test_runtime_audit(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    auditor = ra.RuntimeAuditor(_audit_args(rp))
    assert auditor._load_declared() is not None and auditor.caller_error is None
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    auditor = ra.RuntimeAuditor(_audit_args(rp))
    assert auditor._load_declared() is None
    assert "extra.yaml" in auditor.caller_error


def test_rule_pack_split(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    report = grps.process_rule_packs(str(rp), str(tmp_path / "out"), dry_run=True)
    assert report["validation"]["total_packs"] == 1
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    report = grps.process_rule_packs(str(rp), str(tmp_path / "out2"), dry_run=True)
    assert report["status"] == "error"
    assert any("extra.yaml" in e for e in report["errors"]), report["errors"]
