"""`_lib_consumer_read`'s rule files held as text and in manifests (#3206,
ADR-036 §12): `prometheus_rule_text_results` (the one helper that hands
several inner strings to `da-read prometheus-rules` in one run),
`manifest_rule_file_results` (a ConfigMap / PrometheusRule read the way
kubectl reads it, its rule files the way Prometheus' rule loader reads them)
and `rule_files_in` (#3426: the one classifier of the rule files a file
holds), with the value-level declaration under it.

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


def test_a_list_of_configmaps_is_read_item_by_item(tmp_path) -> None:
    """#3426: the ConfigMaps of a `kind: List` (`kubectl get cm -o yaml`)
    are read as kubectl applies them, one by one."""
    item = _cm(_block("x.yml", RULES), name="listed").replace("\n", "\n  ").rstrip()
    path = _write(tmp_path, "list.yaml", "apiVersion: v1\nkind: List\nitems:\n- " + item + "\n")
    (rf,) = cr.manifest_rule_file_results([path])
    assert (rf.kind, rf.key, rf.problem) == ("ConfigMap", "x.yml", None)
    assert rf.groups[0]["name"]


@pytest.mark.parametrize("doc,reason", [
    ("- " + RULES.replace("\n", "\n  "), "the document is array, not a mapping"),
    ("groups: []\n", "it has no `kind`"),
    ("kind: 3\n", "its `kind` is number, not a string"),
    ("apiVersion: v1\nkind: List\nitems:\n- {kind: ConfigMap}\n",
     "it has no `apiVersion`"),
])
def test_a_document_that_is_no_object_is_named_not_skipped(tmp_path, doc, reason) -> None:
    """#3426: a document of a manifest that is no Kubernetes object used to
    be passed over (`isinstance(doc, dict)`) and the ConfigMap beside it
    read as if it were the whole file. The manifest reading refuses it now,
    naming the document's first line."""
    path = _write(tmp_path, "cm.yaml", _cm(_block("a.yml", RULES)) + "---\n" + doc)
    with pytest.raises(cr.InputRefused) as exc_info:
        cr.manifest_rule_file_results([path])
    assert "cm.yaml" in str(exc_info.value)
    detail = " ".join(exc_info.value.stderr_lines)
    first = 1 + _cm(_block("a.yml", RULES)).count("\n") + 1
    assert f"the document starting at line {first}: " in detail and reason in detail, detail


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
    assert cr._rule_file_declaration(text) == declaration


@pytest.mark.parametrize("text,is_rule_file", [
    (RULES, True), ("route: {}\n", False),
    # undecided: the rule loader loads `groups` from it
    (_INF_RULES, True),
    # undecided and the rule loader refuses it too (a template)
    (_TEMPLATE, False),
])
def test_configmap_value_is_rule_file(text, is_rule_file) -> None:
    assert cr._configmap_value_is_rule_file(text) is is_rule_file


# ── rule_files_in: the one classifier (#3426) ───────────────────────

def _list_of(cm: str) -> str:
    return "apiVersion: v1\nkind: List\nitems:\n- " + cm.replace("\n", "\n  ").rstrip() + "\n"


def test_rule_files_in_reads_every_shape_a_file_comes_in(tmp_path) -> None:
    files = {
        "list.yaml": _list_of(_cm(_block("x.yml", RULES), name="listed")),
        "plain.yaml": RULES,
        "inf.yaml": _INF_RULES,                # only the rule loader reads it
        "pr.yaml": ("apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
                    "metadata: {name: p}\nspec:\n  groups: []\n"),
        "deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: d}\n",
    }
    paths = [_write(tmp_path, n, t) for n, t in files.items()]
    got = cr.rule_files_in(paths)
    lst, plain, inf, pr, deploy = (got[p] for p in paths)
    assert [(rf.kind, rf.key, rf.object_name, rf.problem) for rf in lst] == [
        ("ConfigMap", "x.yml", "listed", None)]
    assert [(rf.kind, rf.manifest, rf.problem) for rf in plain] == [(None, paths[1], None)]
    assert inf[0].problem is None
    assert inf[0].groups[0]["rules"][0]["annotations"] == {"x": ".inf"}
    assert [rf.kind for rf in pr] == ["PrometheusRule"]
    assert deploy == []
    # *kinds* narrows a manifest's rule files
    assert cr.rule_files_in([paths[3]], kinds=("ConfigMap",))[paths[3]] == []


def test_rule_files_in_names_every_file_no_reader_reads(tmp_path) -> None:
    recipe = _write(tmp_path, "recipe.yaml", "recipe: threshold\n")
    broken = _write(tmp_path, "broken.yaml", "groups: [\n")
    bad_cm = _write(tmp_path, "cm.yaml", _cm(_block("a.yml", RULES)) + "---\n- x\n")
    with pytest.raises(cr.RuleFileSourcesRefused) as exc:
        cr.rule_files_in([recipe, broken, bad_cm])
    assert [p for p, _why in exc.value.files] == [recipe, broken, bad_cm]
    why = dict(exc.value.files)
    assert "it is no rule file" in why[recipe]
    assert "Prometheus' rule loader refuses it" in why[broken]
    assert "the document is array" in why[bad_cm]
    # Other YAML may hold no rule file; a manifest kubectl refuses never does;
    # with keep_refusals a file no reader reads is kept, named.
    got = cr.rule_files_in([recipe, broken, bad_cm], other_yaml=cr.NO_RULE_FILES,
                           keep_refusals=True)
    assert got[recipe] == []
    assert [(rf.unread, rf.problem is not None) for rf in got[broken]] == [(True, True)]
    assert [rf.unread for rf in got[bad_cm]] == [True]


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



# ── rule_object_problem: the one judgement of these kinds' shape (#3426) ──

_PR = "apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata: {name: p}\n"


@pytest.mark.parametrize("doc,problem", [
    ({"kind": "PrometheusRule", "metadata": {"name": "p"}, "spec": {"groups": []}}, None),
    ({"kind": "PrometheusRule", "metadata": {"name": "p"}, "groups": []},
     'strict decoding error: unknown field "groups"'),
    ({"kind": "PrometheusRule", "metadata": {"name": "p"}}, "spec: Required value"),
    ({"kind": "PrometheusRule", "metadata": {"name": "p"}, "spec": "x"},
     "spec in body must be of type object"),
    ({"kind": "ConfigMap", "metadata": {"name": "c"}}, None),
    ({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": None}, None),
    ({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": ["x"]}, "its data is list"),
    ({"kind": "ConfigMap", "metadata": {"name": "c"}, "binaryData": "x"}, "its binaryData is str"),
    ({"kind": "ConfigMap", "data": {}}, "no string metadata.name"),
    ({"kind": "Secret", "data": ["x"]}, None),
])
def test_rule_object_problem(doc, problem) -> None:
    got = cr.rule_object_problem(doc)
    assert (got is None) if problem is None else (problem in got), got


def test_a_prometheusrule_without_spec_is_refused_not_empty(tmp_path) -> None:
    """It used to be the rule file `{}` — no rules, no word."""
    path = _write(tmp_path, "pr.yaml", _PR)
    with pytest.raises(cr.InputRefused) as exc:
        cr.manifest_rule_file_results([path])
    assert "spec: Required value" in " ".join(exc.value.stderr_lines)


def test_no_schema_is_no_answer(monkeypatch) -> None:
    """The schema is what judges a PrometheusRule; without it nothing is."""
    monkeypatch.setattr(cr, "_SCHEMA_CACHE", {})
    monkeypatch.setattr(cr, "PROMETHEUSRULE_SCHEMA", "no-such-schema.json")
    monkeypatch.setattr(cr, "_PROMETHEUSRULE_SCHEMA_IN_REPO", ("no", "such", "schema.json"))
    with pytest.raises(cr.ConsumerReadError, match="schema"):
        cr.rule_object_problem({"kind": "PrometheusRule", "metadata": {"name": "p"}})
