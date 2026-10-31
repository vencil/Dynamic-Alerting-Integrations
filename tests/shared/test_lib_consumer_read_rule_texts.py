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

from _platform_fs import symlink_or_skip  # noqa: E402
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
    # a manifest kubectl reads, of no rule-file kind: read, and holds none
    assert deploy == []


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
    # With keep_refusals every file no reader reads is kept, named — the
    # recipe too: no content test lets a file given here go unread (#3426).
    got = cr.rule_files_in([recipe, broken, bad_cm], keep_refusals=True)
    assert [(rf.unread, rf.problem is not None) for rf in got[recipe]] == [(True, True)]
    assert [(rf.unread, rf.problem is not None) for rf in got[broken]] == [(True, True)]
    assert [rf.unread for rf in got[bad_cm]] == [True]


@pytest.mark.parametrize("name,text,why", [
    # #3426 round 4: what no content test told apart. Each is refused,
    # named with kubectl's reason, never taken for a file without rule files.
    ("Chart.yaml", "apiVersion: v2\nname: c\nversion: 0.1.0\n", "it has no `kind`"),
    ("grups.yaml", "grups:\n- name: g\n  rules: []\n", "it has no `kind`"),
    ("no-kind.yaml", "apiVersion: v1\nmetadata: {name: r}\ndata:\n" + _block("a.yml", RULES),
     "it has no `kind`"),
])
def test_rule_files_in_refuses_what_is_no_manifest_and_no_rule_file(tmp_path, name, text,
                                                                   why) -> None:
    path = _write(tmp_path, name, text)
    with pytest.raises(cr.RuleFileSourcesRefused) as exc:
        cr.rule_files_in([path])
    ((got, reason),) = exc.value.files
    assert got == path and why in reason and "it is no rule file" in reason, reason


# ── files_to_read / rule_pack_files: which files are read, by path (#3426) ──

def _tree(tmp_path: Path, names: "list[str]") -> Path:
    for n in names:
        (tmp_path / n).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / n).write_text("x: 1\n", encoding="utf-8")
    return tmp_path


def test_files_to_read_walks_yaml_and_leaves_out_what_exclude_names(tmp_path) -> None:
    root = _tree(tmp_path, ["a.yaml", "b.yml", "notes.md", "helm/c/Chart.yaml",
                            "recipes/r.yaml", "recipes/deep/s.yaml", "x/recipes/t.yaml"])
    rel = lambda got: sorted(Path(g).relative_to(root).as_posix() for g in got)  # noqa: E731
    assert rel(cr.files_to_read([root])) == [
        "a.yaml", "b.yml", "helm/c/Chart.yaml", "recipes/deep/s.yaml", "recipes/r.yaml",
        "x/recipes/t.yaml"]
    # no `/`: the file name, at any depth
    assert "helm/c/Chart.yaml" not in rel(cr.files_to_read([root], ["Chart.yaml"]))
    # a `/`: the path relative to the directory given; `*` crosses `/`
    assert rel(cr.files_to_read([root], ["recipes/*"])) == [
        "a.yaml", "b.yml", "helm/c/Chart.yaml", "x/recipes/t.yaml"]
    # case-sensitive
    assert "helm/c/Chart.yaml" in rel(cr.files_to_read([root], ["chart.yaml"]))
    # a file given as such is read whatever its suffix, unless excluded
    md = str(root / "notes.md")
    assert cr.files_to_read([md]) == [md]
    # #3426 F16: an --exclude that leaves none of the files found is no answer
    with pytest.raises(cr.EverythingExcluded, match="every one excluded"):
        cr.files_to_read([md], ["*.md"])


def test_rule_pack_files_is_a_declaration_that_covers_the_directory(tmp_path) -> None:
    _tree(tmp_path, ["rule-pack-a.yaml", "rule-pack-b.yml", "threshold-registry.yaml",
                     "README.md", "recipes/r.yaml"])
    assert [p.name for p in cr.rule_pack_files(tmp_path)] == [
        "rule-pack-a.yaml", "rule-pack-b.yml"]
    (tmp_path / "pack-c.yaml").write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(cr.RuleFileSourcesRefused) as exc:
        cr.rule_pack_files(tmp_path)
    assert [Path(p).name for p, _why in exc.value.files] == ["pack-c.yaml"]


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
    # #3426 F10: core/v1 ConfigMap's fields only — a data key one indent
    # short is a top-level key, which the API server refuses.
    ({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}, "immutable": True,
      "data": {}, "binaryData": {}}, None),
    ({"kind": "ConfigMap", "metadata": {"name": "c"}, "data": None, "rules.yml": "groups: []"},
     'ConfigMap c: the API server refuses it (strict decoding error: unknown field "rules.yml"'),
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


def test_a_data_key_one_indent_short_is_named_not_skipped(tmp_path) -> None:
    """#3426 F10: `rules.yml: |` beside `data:` (an empty data) used to be a
    ConfigMap with no rule files."""
    path = _write(tmp_path, "cm.yaml", "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: c}\n"
                  "data:\nrules.yml: |\n" + "".join(f"  {ln}\n" for ln in RULES.splitlines()))
    with pytest.raises(cr.RuleFileSourcesRefused) as exc:
        cr.rule_files_in([path])
    assert 'unknown field "rules.yml"' in exc.value.files[0][1]


# ── binaryData: Go's base64.StdEncoding (#3426 F12) ──────────────────

def test_binary_data_folded_over_lines_is_read(tmp_path) -> None:
    """Go's encoding/json decodes a []byte with base64.StdEncoding, which
    skips \\r and \\n: a value folded over lines is what the API server
    stores."""
    b64 = base64.b64encode(RULES.encode()).decode()
    folded = "\r\n".join(b64[i:i + 16] for i in range(0, len(b64), 16))
    assert cr.binary_data_bytes(folded) == RULES.encode()
    block = "".join(f"    {b64[i:i + 16]}\n" for i in range(0, len(b64), 16))
    path = _write(tmp_path, "cm.yaml", _cm(binary_data="  r.yml: |\n" + block))
    (rf,) = cr.manifest_rule_file_results([path])
    assert rf.problem is None and rf.groups[0]["name"] == "g"


@pytest.mark.parametrize("value", [
    "Z3JvdXBzOiBbXQo= ",      # a space
    "Z3JvdXBz\tOiBbXQo=",     # a tab
    "Z3JvdXBz-OiBbXQo=",      # URL-safe alphabet
    "Z3JvdXBzOiBbXQo",        # padding missing
])
def test_binary_data_refuses_every_other_character(tmp_path, value) -> None:
    with pytest.raises((ValueError, base64.binascii.Error)):
        cr.binary_data_bytes(value)
    path = _write(tmp_path, "cm.yaml", _cm(binary_data=f"  r.yml: {value!r}\n".replace("'", '"')))
    (rf,) = cr.manifest_rule_file_results([path])
    assert "not base64" in (rf.problem or ""), rf


# ── #3426 round 5: no object, any-case suffixes, unreadable entries ──

@pytest.mark.parametrize("text", [
    "", "# only a comment\n", "---\n---\n", "apiVersion: v1\nkind: List\nitems: []\n",
])
def test_a_file_with_no_object_and_no_groups_is_refused(tmp_path, text) -> None:
    """F14: kubectl refuses to apply it ("no objects passed to apply"); it
    used to be a file with no rule files."""
    path = _write(tmp_path, "empty.yaml", text)
    with pytest.raises(cr.RuleFileSourcesRefused) as exc:
        cr.rule_files_in([path])
    assert "no objects passed to apply" in exc.value.files[0][1]


def test_files_to_read_takes_any_case_suffix_and_names_what_it_cannot_read(tmp_path) -> None:
    """F15: `.YAML` / `.Yml` are walked; a dangling symlink and a FIFO with a
    YAML suffix are named, never skipped — unless --exclude leaves them out."""
    root = _tree(tmp_path, ["a.YAML", "b.Yml", "c.yaml"])
    assert sorted(Path(f).name for f in cr.files_to_read([root])) == ["a.YAML", "b.Yml", "c.yaml"]
    symlink_or_skip(str(root / "gone.yaml"), str(root / "dangling.yaml"))
    os.mkfifo(str(root / "pipe.yml"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([root])
    assert sorted((Path(p).name, why) for p, why in exc.value.files) == [
        ("dangling.yaml", "a dangling symlink"), ("pipe.yml", "not a regular file")]
    assert len(cr.files_to_read([root], ["dangling.yaml", "pipe.yml"])) == 3


def test_rule_pack_files_names_any_case_suffix_and_a_dangling_pack(tmp_path) -> None:
    """F15: the declaration's refusal of undeclared YAML holds for `.YAML`
    too, and a dangling symlink named like a pack is named."""
    _tree(tmp_path, ["rule-pack-a.yaml", "threshold-registry.yaml"])
    (tmp_path / "rule-pack-b.YAML").write_text("groups: []\n", encoding="utf-8")
    symlink_or_skip(str(tmp_path / "gone"), str(tmp_path / "rule-pack-c.yaml"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.rule_pack_files(tmp_path)
    got = {Path(p).name: why for p, why in exc.value.files}
    assert set(got) == {"rule-pack-b.YAML", "rule-pack-c.yaml"}, got
    assert "neither a rule file" in got["rule-pack-b.YAML"]
    assert got["rule-pack-c.yaml"] == "a dangling symlink"


def test_files_to_read_names_a_target_that_is_no_file_and_no_directory(tmp_path) -> None:
    """A dangling symlink given as a target (an onboard glob returns one)
    is named, not passed over; one that is not there at all adds nothing."""
    ok = _write(tmp_path, "ok.yaml", RULES)
    dangling = str(tmp_path / "dangling.yaml")
    symlink_or_skip(str(tmp_path / "gone.yaml"), dangling)
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([ok, dangling])
    assert exc.value.files == [(dangling, "a dangling symlink")]
    assert cr.files_to_read([ok, str(tmp_path / "absent.yaml")]) == [ok]


def test_an_exclude_that_leaves_only_unread_entries_out_is_everything_excluded(tmp_path) -> None:
    """Excluded entries count as found whether they read or not: excluding
    the one dangling symlink a directory holds is "everything excluded"."""
    root = tmp_path / "d"
    root.mkdir()
    symlink_or_skip(str(root / "gone.yaml"), str(root / "dangling.yaml"))
    with pytest.raises(cr.EverythingExcluded, match="1 file"):
        cr.files_to_read([str(root)], ["dangling.yaml"])


def test_a_symlinked_directory_is_named_not_followed(tmp_path) -> None:
    """os.walk does not follow it (no loop, no walk out of the tree); it
    is named, asking for its target — unless --exclude leaves it out."""
    root = tmp_path / "rules"
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "x.yaml").write_text(RULES, encoding="utf-8")
    root.mkdir()
    (root / "a.yaml").write_text(RULES, encoding="utf-8")
    symlink_or_skip(str(tmp_path / "elsewhere"), str(root / "linked"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(root)])
    ((path, why),) = exc.value.files
    assert path.endswith("linked") and "give its target instead" in why
    assert [Path(f).name for f in cr.files_to_read([str(root)], ["linked"])] == ["a.yaml"]


def _mount(tmp_path: Path, files: "list[str]") -> Path:
    """kubelet's ConfigMap mount: the payload under `..2024_01_01/`,
    `..data -> ..2024_01_01`; the caller adds the user-visible links."""
    mount = tmp_path / "mount"
    for f in files:
        (mount / "..2024_01_01" / f).parent.mkdir(parents=True, exist_ok=True)
        (mount / "..2024_01_01" / f).write_text(RULES, encoding="utf-8")
    symlink_or_skip("..2024_01_01", str(mount / "..data"))
    return mount


def test_a_configmap_mount_is_read_once_by_the_name_the_user_sees(tmp_path) -> None:
    """kubelet's ConfigMap mount: `..data -> ..2024_01_01/` and
    `r.yaml -> ..data/r.yaml`. The payload under `..2024_01_01/` is on a
    hidden path (Go's conf.d walk rule): not read there, and not named,
    because `r.yaml` reads the same file — one file, read once, by the
    path the user sees (#3460)."""
    mount = _mount(tmp_path, ["r.yaml"])
    symlink_or_skip("..data/r.yaml", str(mount / "r.yaml"))
    assert cr.files_to_read([str(mount)]) == [str(mount / "r.yaml")]


def test_an_items_subpath_projection_is_named(tmp_path) -> None:
    """kubelet's `items[].path` with a sub-directory: `team-a ->
    ..data/team-a`. The link's target passes a hidden segment, so the walk
    does not reach it by itself (Go's `dirLinkTargetIsWalked`): named."""
    mount = _mount(tmp_path, ["team-a/x.yaml"])
    symlink_or_skip("..data/team-a", str(mount / "team-a"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(mount)])
    got = {p: why for p, why in exc.value.files}
    assert str(mount / "team-a") in got, got
    assert "give its target instead" in got[str(mount / "team-a")]
    assert "under a hidden path" in got[str(mount / "team-a")]


def test_a_yaml_file_on_a_hidden_path_is_named_unless_excluded(tmp_path) -> None:
    """`.hidden/x.yaml` (and a hidden file `.y.yaml`) is not read, and is
    named — never skipped in silence; `--exclude` lets the run through."""
    root = _tree(tmp_path / "rules", ["a.yaml", ".hidden/x.yaml", ".y.yaml", ".hidden/notes.md"])
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(root)])
    assert sorted((Path(p).relative_to(root).as_posix(), "under a hidden path" in why)
                  for p, why in exc.value.files) == [(".hidden/x.yaml", True), (".y.yaml", True)]
    assert "--exclude" in exc.value.files[0][1]
    assert cr.files_to_read([str(root)], [".hidden/*", ".y.yaml"]) == [str(root / "a.yaml")]


def test_is_hidden_agrees_with_the_go_name_table() -> None:
    """`_is_hidden` is a Python copy of Go's `confdname.IsHidden`; the
    name table Go's matrix test owns pins it, row by row. A table with no
    hidden row, or no visible row, has no detection power and fails."""
    from _confd_name_table import rows  # noqa: PLC0415
    table = rows()
    assert {r["hidden"] for r in table} == {True, False}, \
        "the name table needs hidden and visible rows to pin `_is_hidden`"
    assert [(r["name"], cr._is_hidden(r["name"])) for r in table] == \
        [(r["name"], r["hidden"]) for r in table]
    # `_is_yaml_name` is Go's `SplitCarrier` suffix rule (`.yaml` itself included)
    assert {r["yaml_extension"] for r in table} == {True, False}
    assert [(r["name"], cr._is_yaml_name(r["name"])) for r in table] == \
        [(r["name"], r["yaml_extension"]) for r in table]


def test_a_non_regular_yaml_entry_on_a_hidden_path_is_named(tmp_path) -> None:
    """One classification for every entry: a dangling symlink or a FIFO
    with a YAML name cannot be read on a hidden path either — named as
    it is on a visible one, never dropped."""
    root = _tree(tmp_path / "rules", ["a.yaml", ".h/keep.md"])
    symlink_or_skip(str(root / "gone.yaml"), str(root / ".h" / "dangling.yaml"))
    symlink_or_skip(str(root / "gone.yaml"), str(root / ".d.yaml"))
    os.mkfifo(str(root / ".h" / "pipe.yml"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(root)])
    assert sorted((Path(p).relative_to(root).as_posix(), why) for p, why in exc.value.files) == [
        (".d.yaml", "a dangling symlink"), (".h/dangling.yaml", "a dangling symlink"),
        (".h/pipe.yml", "not a regular file")]


def test_a_file_named_only_by_its_suffix_is_a_yaml_name(tmp_path) -> None:
    """`.yaml` / `.YML` carry a YAML suffix (Go's `SplitCarrier`); hidden,
    so not read — and named, not dropped as "no YAML"."""
    root = _tree(tmp_path / "rules", ["a.yaml", ".yaml", "sub/.YML"])
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(root)])
    assert sorted(Path(p).relative_to(root).as_posix() for p, _ in exc.value.files) == [
        ".yaml", "sub/.YML"]


def test_overlapping_targets_name_a_hidden_file_once(tmp_path) -> None:
    """`[t, t/s]` both reach `t/s/.h/x.yaml`: one finding, not two."""
    t = _tree(tmp_path / "t", ["a.yaml", "s/b.yaml", "s/.h/x.yaml"])
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(t), str(t / "s")])
    assert exc.value.files == [(str(t / "s" / ".h" / "x.yaml"), cr._UNDER_HIDDEN)]


def test_a_file_target_on_a_hidden_path_is_read_as_given(tmp_path) -> None:
    """The hidden rule judges paths below a directory target, never a
    target given as a file."""
    f = _tree(tmp_path, [".github/x.yaml"]) / ".github" / "x.yaml"
    assert cr.files_to_read([str(f)]) == [str(f)]


def test_a_hidden_directory_target_is_walked(tmp_path) -> None:
    """The target itself is never judged by its name: `..data` given as a
    target is walked (relative to it, nothing is hidden)."""
    mount = _mount(tmp_path, ["r.yaml"])
    assert cr.files_to_read([str(mount / "..data")]) == [str(mount / "..data" / "r.yaml")]


def test_two_names_for_one_file_are_each_read(tmp_path) -> None:
    """Two non-hidden symlinks to one file are two paths, each read — as
    the exporter reads them."""
    root = _tree(tmp_path / "rules", ["real.yaml"])
    symlink_or_skip("real.yaml", str(root / "a.yaml"))
    symlink_or_skip("real.yaml", str(root / "b.yaml"))
    assert [Path(f).name for f in cr.files_to_read([str(root)])] == ["a.yaml", "b.yaml", "real.yaml"]


def test_a_hidden_directory_symlink_out_of_the_tree_is_named(tmp_path) -> None:
    """`..data -> <outside>`: Go passes every hidden name over in silence;
    this walk names it, since nothing behind it would be read."""
    _tree(tmp_path / "elsewhere", ["x.yaml"])
    root = _tree(tmp_path / "rules", ["a.yaml"])
    symlink_or_skip(str(tmp_path / "elsewhere"), str(root / "..data"))
    with pytest.raises(cr.SourcesUnlisted) as exc:
        cr.files_to_read([str(root)])
    ((path, why),) = exc.value.files
    assert path == str(root / "..data") and "give its target instead" in why


def test_a_symlink_back_into_the_tree_is_no_loop_and_no_finding(tmp_path) -> None:
    """`loop -> .`: not followed (no endless walk), not named (in the tree)."""
    root = tmp_path / "rules"
    root.mkdir()
    (root / "a.yaml").write_text(RULES, encoding="utf-8")
    symlink_or_skip(".", str(root / "loop"))
    assert [Path(f).name for f in cr.files_to_read([str(root)])] == ["a.yaml"]
