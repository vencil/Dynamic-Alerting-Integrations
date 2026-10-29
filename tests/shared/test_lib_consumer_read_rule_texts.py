"""`_lib_consumer_read`'s rule files held as text and in manifests (#3206,
ADR-036 §12): `prometheus_rule_text_results` (the one helper that hands
several inner strings to `da-read prometheus-rules` in one run),
`manifest_rule_file_results` (a ConfigMap / PrometheusRule read the way
kubectl reads it, its rule files the way Prometheus' rule loader reads them)
and `rule_file_declaration`.

The last part pins what changed for every migrated reader when the reading
moved from PyYAML (YAML 1.1) to rulefmt (go-yaml v3 into Prometheus' typed
structs): each case is one value whose reading differs, measured on the
binary, with what PyYAML said beside it.
"""
from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

import pytest
import yaml

import _lib_consumer_read as cr

RULES = "groups:\n- name: g\n  rules:\n  - {alert: A, expr: up == 0}\n"
BAD_PROMQL = "groups:\n- name: g\n  rules:\n  - {alert: B, expr: 'rate(x[5m]'}\n"


def _write(tmp_path: Path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# ── prometheus_rule_text_results ──────────────────────────────────────

def test_texts_are_read_in_one_run_under_their_names() -> None:
    got = cr.prometheus_rule_text_results([("cm.yaml#a.yml", RULES), ("cm.yaml#b.yml", b"")])
    assert [r.path for r in got] == ["cm.yaml#a.yml", "cm.yaml#b.yml"]
    assert got[0].groups[0]["rules"] == [{"alert": "A", "expr": "up == 0"}]
    assert got[1].documents == [{}] and got[1].groups == []
    assert cr.prometheus_rule_text_results([]) == []


def test_a_refused_text_keeps_its_name_and_reason() -> None:
    ok, bad = cr.prometheus_rule_text_results([("x#ok", RULES), ("x#bad", BAD_PROMQL)])
    assert ok.problem is None
    assert bad.path == "x#bad" and "parse error" in bad.problem
    with pytest.raises(cr.RuleFileRefused) as exc_info:
        bad.groups
    assert exc_info.value.path == "x#bad"
    with pytest.raises(cr.RuleFilesRefused):
        cr.require_read([ok, bad])


def test_the_temporary_directory_is_private_and_removed(monkeypatch) -> None:
    seen = {}
    real = cr.prometheus_rule_file_results

    def spy(paths, binary=None):
        paths = list(paths)
        d = os.path.dirname(paths[0])
        seen["dir"], seen["mode"] = d, stat.S_IMODE(os.stat(d).st_mode)
        seen["files"] = [stat.S_IMODE(os.stat(p).st_mode) for p in paths]
        return real(paths, binary)

    monkeypatch.setattr(cr, "prometheus_rule_file_results", spy)
    cr.prometheus_rule_text_results([("a", RULES), ("b", RULES)])
    assert seen["mode"] == 0o700
    assert seen["files"] == [0o600, 0o600]
    assert not os.path.exists(seen["dir"])


def test_texts_without_da_read_fail_by_name(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("DA_READ_BINARY", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(cr.ConsumerReadError, match="da-read was not found") as exc_info:
        cr.prometheus_rule_text_results([("a", RULES)])
    assert not isinstance(exc_info.value, cr.InputRefused)


# ── manifest_rule_file_results ────────────────────────────────────────

def _cm(data: str = "", binary_data: str = "", name: str = "r") -> str:
    out = f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}}}\n"
    if data:
        out += "data:\n" + data
    if binary_data:
        out += "binaryData:\n" + binary_data
    return out


def _block(key: str, text: str) -> str:
    return f"  {key}: |\n" + "".join(f"    {ln}\n" for ln in text.splitlines())


def test_every_data_and_binary_value_of_every_configmap(tmp_path) -> None:
    b64 = base64.b64encode(RULES.encode()).decode()
    text = (_cm(_block("a.yml", RULES), f"  c.yml: {b64}\n  bad.yml: '!!notb64'\n")
            + "---\n" + _cm(_block("b.yml", BAD_PROMQL), name="s")
            + "---\napiVersion: v1\nkind: Secret\nmetadata: {name: x}\n")
    path = _write(tmp_path, "cm.yaml", text)
    got = cr.manifest_rule_file_results([path])
    assert [(r.kind, r.key, r.path) for r in got] == [
        ("ConfigMap", "a.yml", f"{path}#a.yml"), ("ConfigMap", "c.yml", f"{path}#c.yml"),
        ("ConfigMap", "bad.yml", f"{path}#bad.yml"), ("ConfigMap", "b.yml", f"{path}#b.yml")]
    assert all(r.manifest == path for r in got)
    a, c, bad, b = got
    assert a.groups == c.groups and a.problem is None
    assert "not base64" in bad.problem
    assert "parse error" in b.problem


def test_a_prometheusrule_spec_is_the_rule_file_the_operator_writes(tmp_path) -> None:
    path = _write(tmp_path, "pr.yaml", (
        "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
        "metadata: {name: da-rule-pack-x}\nspec:\n  groups:\n  - name: g\n"
        "    rules:\n    - {alert: A, expr: up == 0, for: 60s}\n"))
    (rf,) = cr.manifest_rule_file_results([path])
    assert (rf.kind, rf.key) == ("PrometheusRule", "spec")
    assert rf.groups[0]["rules"][0]["for"] == "1m"   # Prometheus' spelling


def test_only_declared_skips_values_that_are_no_rule_file(tmp_path) -> None:
    text = _cm(_block("alertmanager.yml", "route: {receiver: x}\n")
               + _block("notes.md", "# - alert: example\n*markdown*\n")
               + _block("rules.yml", RULES))
    path = _write(tmp_path, "cm.yaml", text)
    assert [r.key for r in cr.manifest_rule_file_results([path], only_declared=True)] == [
        "rules.yml"]
    every = cr.manifest_rule_file_results([path])
    assert [r.key for r in every if r.problem is not None] == ["alertmanager.yml", "notes.md"]


def test_a_manifest_kubectl_refuses_is_named(tmp_path) -> None:
    path = _write(tmp_path, "bad.yaml", "a: [unclosed\n")
    with pytest.raises(cr.InputRefused, match="bad.yaml"):
        cr.manifest_rule_file_results([path])


_INF_RULES = ("groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n"
              "    annotations: {x: .inf}\n")
_TAB_BROKEN = "groups:\n  - name: x\n    rules:\n\t- alert: BrokenTabAlert\n      expr: up == 0\n"
_TEMPLATE = "# - alert: example\n*markdown*\n"


@pytest.mark.parametrize("text,declaration", [
    (RULES, cr.RULE_FILE), ("groups: []\n", cr.RULE_FILE),
    ("route: {}\n", cr.NOT_A_RULE_FILE), ("", cr.NOT_A_RULE_FILE),
    ("- a\n- b\n", cr.NOT_A_RULE_FILE),
    # kubectl's reading refuses these, so it cannot say — not "no"
    ("not: [yaml\n", cr.UNDECIDED), (_INF_RULES, cr.UNDECIDED),
    (_TAB_BROKEN, cr.UNDECIDED), (_TEMPLATE, cr.UNDECIDED),
])
def test_rule_file_declaration(text, declaration) -> None:
    assert cr.rule_file_declaration(text) == declaration
    assert cr.may_be_rule_file(text) is (declaration != cr.NOT_A_RULE_FILE)


@pytest.mark.parametrize("text,is_rule_file", [
    (RULES, True), ("route: {}\n", False),
    # undecided: the rule loader loads `groups` from it
    (_INF_RULES, True),
    # undecided and the rule loader refuses it too (a template)
    (_TEMPLATE, False),
])
def test_configmap_value_is_rule_file(text, is_rule_file) -> None:
    assert cr.configmap_value_is_rule_file(text) is is_rule_file


# ── what the reading changed (PyYAML YAML 1.1 → rulefmt) ─────────────

def _rule(fields: str) -> str:
    return f"groups:\n- name: g\n  rules:\n  - alert: A\n    expr: up == 0\n{fields}"


@pytest.mark.parametrize("fields,key,pyyaml,rulefmt", [
    # durations are Prometheus' model.Duration, printed canonically
    ("    for: 60s\n", "for", "60s", "1m"),
    ("    for: 90s\n", "for", "90s", "1m30s"),
    # label / annotation values are strings, spelled as written
    ("    labels: {tenant: 010}\n", "labels", {"tenant": 8}, {"tenant": "010"}),
    ("    labels: {page: true}\n", "labels", {"page": True}, {"page": "true"}),
    ("    labels: {on: yes}\n", "labels", {True: True}, {"on": "yes"}),
    ("    labels: {d: 2001-12-14}\n", "labels", None, {"d": "2001-12-14"}),
    ("    labels: {n: null}\n", "labels", {"n": None}, {"n": ""}),
    ("    annotations: {v: 1e3}\n", "annotations", {"v": "1e3"}, {"v": "1e3"}),
])
def test_values_read_as_prometheus_reads_them(fields, key, pyyaml, rulefmt) -> None:
    text = _rule(fields)
    (rf,) = cr.prometheus_rule_text_results([("t", text)])
    assert rf.problem is None, rf.problem
    assert rf.groups[0]["rules"][0][key] == rulefmt
    if pyyaml is not None:
        assert yaml.safe_load(text)["groups"][0]["rules"][0][key] == pyyaml


@pytest.mark.parametrize("text,why", [
    # a bare number is no duration to Prometheus; PyYAML read 60
    ("groups:\n- name: g\n  interval: 60\n  rules: []\n", 'not a valid duration string: "60"'),
    # a key Prometheus does not know (vmalert's `type`, a typo) is refused
    ("groups:\n- name: g\n  type: prometheus\n  rules: []\n", "field type not found"),
    # PromQL is parsed
    (BAD_PROMQL, "parse error"),
    # the rule loader takes one document
    (RULES + "---\n" + RULES, "document 2"),
])
def test_files_pyyaml_read_and_prometheus_refuses(text, why) -> None:
    yaml.safe_load_all(text)          # PyYAML read all of these
    (rf,) = cr.prometheus_rule_text_results([("t", text)])
    assert rf.problem is not None and why in rf.problem, rf.problem
