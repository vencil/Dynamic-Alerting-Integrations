"""The ops tools that read a rule-packs directory take its rule packs from the
one declaration, `_lib_consumer_read.RULE_PACKS` (#3426) — for
`operator-generate`, `operator-check` and `runtime-audit` as the selector
their `--rule-packs-dir` is declared with (`_lib_input.add_input`, #3462).

Each of `operator-generate`, `operator-check`, `runtime-audit` and
`rule-pack-split` used to `glob("rule-pack-*.yaml")` on its own: a `.yml`
pack was not seen, and a YAML beside the packs that is neither a pack nor
declared there was passed over in silence. Each case below turns red if its
tool goes back to a glob of its own.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import _lib_consumer_read as cr
import _lib_input
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


def _selected(parser, rp):
    """What the shipped parser's --rule-packs-dir selects from *rp*."""
    return _lib_input.selected(parser.parse_args(["--rule-packs-dir", str(rp)]),
                               "rule_packs_dir", lang="en")


def test_operator_generate(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    assert [p.name for p in _selected(og.build_arg_parser(), rp)] == ["rule-pack-x.yml"]
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    with pytest.raises(cr.SourcesUnlisted, match="cannot be chosen"):
        _selected(og.build_arg_parser(), rp)


def _rules_check(rule_packs):
    checker = oc.OperatorChecker(SimpleNamespace(
        namespace="monitoring", rule_packs_dir="rp", prometheus=None,
        kubeconfig=None, json=False, ci=False), rule_packs)
    checker._installed = lambda crd: False
    return checker.check_prometheus_rule_status()


def test_operator_check(tmp_path, monkeypatch, capsys):
    rp = _rule_packs(tmp_path, extra=False)
    packs = cr.rule_pack_files(rp)
    assert [p.name for p in packs] == ["rule-pack-x.yml"]
    assert _rules_check(packs).detail.startswith("0/1")   # the .yml pack is expected
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    # judged in main, before the cluster is asked: rc 2 naming the entry
    monkeypatch.setattr("sys.argv", ["operator_check.py", "--rule-packs-dir", str(rp)])
    monkeypatch.setattr(oc, "OperatorChecker", None)   # never reached
    with pytest.raises(SystemExit) as exc:
        oc.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--rule-packs-dir" in err and "extra.yaml" in err


def _audit_args(rp):
    return ra.build_parser().parse_args(["--rule-packs-dir", str(rp)])


def test_runtime_audit(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    auditor = ra.RuntimeAuditor(_audit_args(rp))
    assert auditor._load_declared() is not None and auditor.caller_error is None
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    auditor = ra.RuntimeAuditor(_audit_args(rp))
    assert auditor._load_declared() is None
    assert "extra.yaml" in auditor.caller_error
    # the directory is there and not empty: the message says what it holds
    assert "not found or empty" not in auditor.caller_error
    assert "declaration does not cover" in auditor.caller_error


def test_operator_generate_main_exits_2_on_an_undeclared_yaml(tmp_path, monkeypatch, capsys):
    """`main` turns `SourcesUnlisted` into rc 2 naming the file
    (`rule_packs_unreadable`), never a CRD set without it."""
    rp = _rule_packs(tmp_path, extra=True)
    monkeypatch.setattr("sys.argv", ["operator_generate.py", "--rule-packs-dir", str(rp),
                                     "--dry-run"])
    with pytest.raises(SystemExit) as exc:
        og.main()
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "extra.yaml" in err and "rule packs" in err


def test_rule_pack_split(tmp_path):
    rp = _rule_packs(tmp_path, extra=False)
    report = grps.process_rule_packs(str(rp), str(tmp_path / "out"), dry_run=True)
    assert report["validation"]["total_packs"] == 1
    (rp / "extra.yaml").write_text(_PACK, encoding="utf-8")
    report = grps.process_rule_packs(str(rp), str(tmp_path / "out2"), dry_run=True)
    assert report["status"] == "error"
    assert any("extra.yaml" in e for e in report["errors"]), report["errors"]


def test_operator_generate_names_a_symlinked_pack_by_its_own_name(tmp_path, monkeypatch):
    """A pack's name is its file's own name (`RULE_PACKS.member`): a
    rule-pack-x.yaml that is a symlink keeps `x`, not its target's name
    (PR #3610 review: the selected files were resolved one by one)."""
    from _platform_fs import symlink_or_skip
    rp = tmp_path / "rule-packs"
    rp.mkdir()
    (tmp_path / "source.yaml").write_text(_PACK, encoding="utf-8")
    symlink_or_skip(tmp_path / "source.yaml", rp / "rule-pack-linked.yaml")
    monkeypatch.setattr("sys.argv", ["operator_generate.py", "--rule-packs-dir", str(rp),
                                     "--components", "rules", "--dry-run", "--json"])
    report = og.main()
    names = [c["metadata"]["name"] for c in report.document["crds"]]
    assert any("linked" in n for n in names), names
    assert not any("source" in n for n in names), names
