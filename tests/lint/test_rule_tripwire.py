"""The rule-source declaration, chart values registration and the coverage tripwire (#3438).

Which files hold the repo's rule files is declared by path
(`_lib_consumer_read.RULE_SOURCES`, `CHART_VALUES_RULES`) and read by
`rule_files_in`. Rule-shaped YAML anywhere else rings `_rule_tripwire` — an
alarm whose answer is messages, which no reader may take files from.
"""
from __future__ import annotations

import ast
import sys
import typing
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"
sys.path.insert(0, str(TOOLS / "lint"))
sys.path.insert(0, str(TOOLS))

import _lib_consumer_read as cr  # noqa: E402
import _rule_tree  # noqa: E402
import _rule_tripwire  # noqa: E402

RULES = {"groups": [{"name": "g", "rules": [{"alert": "FromValues", "expr": "up == 0"}]}]}


def _imports_tripwire(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
                a.name.split(".")[-1] == "_rule_tripwire" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "_rule_tripwire":
            return True
        if isinstance(node, ast.Constant) and node.value == "_rule_tripwire":
            return True      # importlib.import_module("_rule_tripwire") and the like
    return False


def test_no_tool_imports_the_tripwire():
    """⛔ The tripwire's hits must never become files a reader reads: that
    would be the content-based discovery the declaration replaced. So no
    module under scripts/tools imports it — only the contract tests call it."""
    importers = sorted(p.relative_to(TOOLS).as_posix() for p in TOOLS.rglob("*.py")
                       if p.name != "_rule_tripwire.py"
                       and _imports_tripwire(ast.parse(p.read_text(encoding="utf-8"))))
    assert importers == [], (
        f"{importers} import _rule_tripwire: its answer is an alarm for a test, "
        "never a list of files to read — declare the files in "
        "_lib_consumer_read.RULE_SOURCES instead")


def test_the_search_finds_an_import():
    """Control for the search above."""
    for src in ("import _rule_tripwire\n", "from _rule_tripwire import x\n",
                "import importlib\nimportlib.import_module('_rule_tripwire')\n"):
        assert _imports_tripwire(ast.parse(src)), src
    assert not _imports_tripwire(ast.parse("import _rule_tree\n"))


def test_the_tripwire_answers_messages_not_files(tmp_path):
    """Its declared return type is a tuple of strings, and so is what it returns."""
    hints = typing.get_type_hints(_rule_tripwire.rule_shaped_outside_declaration)
    assert hints["return"] == tuple[str, ...], hints["return"]
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "x.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")
    got = _rule_tripwire.rule_shaped_outside_declaration(str(tmp_path), ["k8s/x.yaml"],
                                                         frozenset())
    assert isinstance(got, tuple) and len(got) == 1 and isinstance(got[0], str), got
    assert got[0].startswith("k8s/x.yaml: ") and "RULE_SOURCES" in got[0], got


def test_declared_files_are_not_judged(tmp_path):
    """The tripwire skips what is declared: rule files, and `NOT_RULE_FILES`
    (each with its reason) — nothing else."""
    (tmp_path / "k8s").mkdir()
    (tmp_path / "k8s" / "x.yaml").write_text(yaml.safe_dump(RULES), encoding="utf-8")
    assert _rule_tripwire.rule_shaped_outside_declaration(
        str(tmp_path), ["k8s/x.yaml"], frozenset({"k8s/x.yaml"})) == ()
    assert _rule_tripwire.declared_not_rules() == frozenset(
        p for p, _why in cr.NOT_RULE_FILES)
    assert all(why for _p, why in cr.NOT_RULE_FILES)


def test_yaml_listed_beside_rule_files_is_still_judged(tmp_path):
    """⛔ `not_rule_files` only says which YAML beside the rule files is
    accounted for; it is no exemption. A rules ConfigMap appended to
    configmap-grafana.yaml (listed there) rings the tripwire."""
    rel = "k8s/03-monitoring/configmap-grafana.yaml"
    assert "configmap-grafana.yaml" in cr.CONFIGMAP_RULES.not_rule_files
    text = (REPO_ROOT / rel).read_text(encoding="utf-8") + (
        "\n---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: hidden}\n"
        "data:\n  r.yml: |\n    groups:\n      - name: g\n        rules:\n"
        "          - alert: Hidden\n            expr: up == 0\n")
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(text, encoding="utf-8")
    rung = _rule_tripwire.rule_shaped_outside_declaration(str(tmp_path), [rel], frozenset())
    assert len(rung) == 1 and rung[0].startswith(rel + ":"), rung


def test_stale_declaration_entries_are_named(tmp_path, monkeypatch):
    """An entry that names nothing is not left in silence."""
    assert cr.stale_rule_source_entries(REPO_ROOT) == []
    monkeypatch.setattr(cr, "NOT_RULE_FILES", (("k8s/gone.yaml", "why"),))
    monkeypatch.setattr(cr, "RULE_SOURCES", (cr.RuleSource("k8s", ("x-*.yaml",), ("gone.yaml",)),))
    monkeypatch.setattr(cr, "CHART_VALUES_RULES", (
        cr.ChartValuesRules("helm/gone/values.yaml", ("a",), cr.RULE_FILE_VALUE),))
    (tmp_path / "k8s").mkdir()
    got = cr.stale_rule_source_entries(tmp_path)
    assert len(got) == 3, got
    assert any("not_rule_files" in m for m in got)
    assert any("NOT_RULE_FILES" in m for m in got)
    assert any("CHART_VALUES_RULES" in m and "not there" in m for m in got)


def test_every_declared_place_is_inside_the_tripwire_scope():
    """⛔ The tripwire scans `_SHIPPED_ROOTS`; a rule source or a registered
    values file outside them would be read while nothing guards its
    neighbourhood — the scan and the declaration would come apart."""
    roots = _rule_tree._SHIPPED_ROOTS
    places = [s.directory + "/" for s in cr.RULE_SOURCES] + [
        r.values for r in cr.CHART_VALUES_RULES]
    outside = [p for p in places if not p.startswith(roots)]
    assert not outside, f"{outside} are outside the tripwire's scope {roots}"


def test_rule_pack_files_is_the_rule_packs_entry():
    """No second glob: `rule_pack_files` is `RULE_PACKS` applied to a directory,
    and the repo's declared rule packs are exactly what it lists."""
    packs = cr.rule_pack_files(REPO_ROOT / cr.RULE_PACKS.directory)
    declared = [p for p in cr.repo_rule_files(REPO_ROOT)
                if p.parent == REPO_ROOT / cr.RULE_PACKS.directory]
    assert packs == declared and packs
    assert cr.RULE_PACKS in cr.RULE_SOURCES


# ── chart values registration (breaking, #3438) ─────────────────────────────


def _chart_tree(root: Path, values: dict) -> Path:
    for src in cr.RULE_SOURCES:
        d = root / src.directory
        d.mkdir(parents=True, exist_ok=True)
        (d / src.rule_files[0].replace("*", "a")).write_text("groups: []\n", encoding="utf-8")
    chart = root / "helm" / "c"
    chart.mkdir(parents=True)
    (chart / "values.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    return root


@pytest.fixture
def registered(monkeypatch):
    def register(*regs):
        monkeypatch.setattr(cr, "CHART_VALUES_RULES", tuple(regs))
    yield register
    _rule_tree._reset_caches()


def test_a_registered_values_key_is_read_into_the_contracts(tmp_path, registered, monkeypatch):
    root = _chart_tree(tmp_path, {"prometheus": {"extraRules": RULES}, "other": RULES})
    registered(cr.ChartValuesRules("helm/c/values.yaml", ("prometheus", "extraRules"),
                                   cr.RULE_FILE_VALUE))
    got = cr.chart_values_rule_files(root)
    assert [(r.manifest, r.key, r.problem) for r in got] == [
        ("helm/c/values.yaml", "prometheus.extraRules", None)]
    assert got[0].groups[0]["rules"][0]["alert"] == "FromValues"
    # …into every contract: `_rule_tree`'s containers.
    monkeypatch.setattr(_rule_tree, "_REPO_ROOT", str(root))
    _rule_tree._reset_caches()
    where = {w: d for w, d, _p in _rule_tree._rule_containers()}
    assert "helm/c/values.yaml:prometheus.extraRules" in where, sorted(where)
    assert {r["alert"] for _w, r in _rule_tree._iter_repo_alert_rules()} == {"FromValues"}
    # The registered key is no alarm; the unregistered one beside it is.
    rung = _rule_tripwire.rule_shaped_outside_declaration(
        str(root), ["helm/c/values.yaml"], frozenset())
    assert len(rung) == 1 and "`other.groups`" in rung[0], rung


def test_a_registration_that_reads_nothing_is_named(tmp_path, registered):
    root = _chart_tree(tmp_path, {"prometheus": {}})
    registered(cr.ChartValuesRules("helm/c/values.yaml", ("prometheus", "extraRules"),
                                   cr.RULE_FILE_VALUE),
               cr.ChartValuesRules("helm/c/values.yaml", ("prometheus",), cr.RULE_FILE_VALUE),
               cr.ChartValuesRules("helm/c/values.yaml", ("prometheus",), "rules"))
    got = cr.chart_values_rule_files(root)
    assert got[0].unread and "not in the values file" in got[0].problem, got
    assert got[2].unread and "neither" in got[2].problem, got
    # `{}` is no rule file the loader reads groups from: it reads, zero groups —
    # `_rule_tree`'s sentinel names that container as empty.
    assert got[1].problem is None and got[1].groups == [], got


def test_a_registered_rule_list_is_read_as_the_group_the_template_makes(
        tmp_path, registered, monkeypatch):
    """F4: a bare rule list (what a template puts under a group's `rules:`)
    rings the tripwire, and once registered as `RULE_LIST_VALUE` it is read
    — wrapped in one group, the way the template renders it — and reaches
    the contracts. Unwrapped, the rule loader refuses a list (`!!seq`)."""
    rules = RULES["groups"][0]["rules"]
    root = _chart_tree(tmp_path, {"extraRules": rules})
    rung = _rule_tripwire.rule_shaped_outside_declaration(
        str(root), ["helm/c/values.yaml"], frozenset())
    assert len(rung) == 1 and "`extraRules`" in rung[0], rung
    registered(cr.ChartValuesRules("helm/c/values.yaml", ("extraRules",), cr.RULE_LIST_VALUE))
    (got,) = cr.chart_values_rule_files(root)
    assert got.problem is None, got
    assert got.groups == [{"name": "helm/c/values.yaml#extraRules", "rules": rules}]
    monkeypatch.setattr(_rule_tree, "_REPO_ROOT", str(root))
    _rule_tree._reset_caches()
    assert {r["alert"] for _w, r in _rule_tree._iter_repo_alert_rules()} == {"FromValues"}
    assert _rule_tripwire.rule_shaped_outside_declaration(
        str(root), ["helm/c/values.yaml"], frozenset()) == ()
    # …and as a rule file it is refused: the shape is part of the reading.
    registered(cr.ChartValuesRules("helm/c/values.yaml", ("extraRules",), cr.RULE_FILE_VALUE))
    (got,) = cr.chart_values_rule_files(root)
    assert got.problem is not None, got


def test_no_chart_values_are_registered_today_and_none_ring():
    """Today's repo: no registration, and the tripwire rings nowhere
    (the contract test asserts the same on the live tree)."""
    assert cr.CHART_VALUES_RULES == ()
    assert cr.chart_values_rule_files(REPO_ROOT) == []


def test_a_template_that_spells_out_recording_rules_rings(tmp_path):
    """Where no reading reads the text (a Helm template), the regex is the
    fallback — and it knows recording rules as well as alerts."""
    (tmp_path / "helm" / "c" / "templates").mkdir(parents=True)
    rel = "helm/c/templates/rules.yaml"
    (tmp_path / rel).write_text(
        "groups:\n  - name: {{ .Release.Name }}\n    rules:\n"
        "      - record: job:up:sum\n        expr: sum(up)\n", encoding="utf-8")
    rung = _rule_tripwire.rule_shaped_outside_declaration(str(tmp_path), [rel], frozenset())
    assert len(rung) == 1 and "- record:" in rung[0], rung
