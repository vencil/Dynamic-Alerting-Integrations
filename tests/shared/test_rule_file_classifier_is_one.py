"""Which rule files a file holds has ONE answer: `_lib_consumer_read.rule_files_in` (#3426).

Each tool used to decide it from a reading of its own — "is there a `groups`
key in what da-crdecode gave me?" — and each decided it differently: one
skipped a ConfigMap inside a `kind: List`, another looked only at the first
document and skipped every ConfigMap. Fixing them one at a time left the next
one wrong. So the decision lives in one function, and this test keeps it
there.

What it looks for, with the AST of every module under `scripts/tools/`: a
module that reads YAML through da-crdecode (a `kubernetes_*` / `any_yaml_*`
reading of `_lib_consumer_read`) AND decides on the `groups` key — `"groups"
in X`, or `X.get("groups")` / `X["groups"]` as a condition (an `if` / `while`
/ ternary test, a comprehension filter, an `any()` / `all()` argument). A
module that only reads rule files Prometheus' rule loader already read
(`prometheus_rule_file_results`, `rule_files_in`) and checks their `groups`
is not a classifier and is not matched: those files are rule files already.

Measured when this was written: without the da-crdecode condition the same
search matches 7 modules — 6 of them read only the rule loader's results
(`generate_alert_reference`, `inject_waveform`, `check_metric_dictionary`,
`check_metric_group_pairs`, `operator_generate`, and `check_admin_config_schema`,
whose `groups` is the RBAC config's) — so the condition is what makes the
search mean "classifies from a da-crdecode reading". With it, the matches were
three; `_rule_tree` left the list when its files became the rule-source
declaration's, read by `rule_files_in` (#3438). The two left are named below,
each with why it stays. (`lint/_rule_tripwire.py` reads with da-crdecode and
looks for rule SHAPES, but decides on no `groups` key the way this search
means, so it is not matched; it is an alarm that feeds no reader — see
tests/lint/test_rule_tripwire.py.)

⚠️ A tripwire, not a proof. It is cheap and catches the shapes the replaced
classifiers were written in; it does not catch a classifier written another
way, and it is not to be grown into one that tries (that direction was tried
and abandoned). Known misses, among others:

* the decision taken off a condition: `ok = "groups" in doc` … `if ok:` is
  caught (the membership test), but `has = doc.get("groups")` … `if has:`
  is not (the access is not itself the condition);
* the key spelled through a variable or a constant (`doc.get(KEY)`), or
  built (`"gro" + "ups"`);
* other spellings of the same test: `set(doc) & {"groups"}`, `match doc:
  case {"groups": _}`, `try: doc["groups"] except KeyError`,
  `operator.itemgetter("groups")`, `dict.get(doc, "groups")`;
* the decision made in a helper of another module that does not itself read
  through da-crdecode;
* a module that reads YAML some other way (the text, a regex on it) and
  then decides;
* the narrowing itself: a module that classifies from a da-crdecode reading
  it was HANDED (no `kubernetes_*` / `any_yaml_*` call of its own) is not
  matched, and a module that both reads through da-crdecode and checks the
  `groups` of rule-loader results is matched although it classifies nothing
  (the two false positives below).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "scripts" / "tools"
ENTRY = TOOLS / "_lib_consumer_read.py"

#: Modules allowed to match, each with its reason. An entry that no longer
#: matches fails the test (the list only shrinks).
ALLOWED = {
    "lint/check_admin_config_schema.py":
        "`config.get(\"groups\")` is the RBAC config's `groups` (tenant-api's "
        "decoded config, not a rule file); it reads Helm values with da-crdecode",
    "lint/check_orphan_recordings.py":
        "reads Grafana dashboards' ConfigMaps with da-crdecode; its `groups` "
        "test is on rule files `manifest_rule_file_results` / the rule loader "
        "already read",
}

_READINGS = ("kubernetes_documents", "kubernetes_documents_of", "kubernetes_text_documents",
             "kubernetes_text_documents_each", "any_yaml_documents", "any_yaml_documents_of",
             "any_yaml_text_documents", "any_yaml_text_documents_each")


def _is_groups(n: ast.AST) -> bool:
    return isinstance(n, ast.Constant) and n.value == "groups"


def _groups_access(n: ast.AST) -> bool:
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
            and n.args and _is_groups(n.args[0])):
        return True
    return isinstance(n, ast.Subscript) and _is_groups(n.slice)


def _conditions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            yield node.test
        elif isinstance(node, ast.comprehension):
            yield from node.ifs
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("any", "all")):
            yield from node.args


def groups_decisions(tree: ast.AST) -> "list[int]":
    """Line numbers where *tree* decides on a `groups` key."""
    lines = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Compare) and _is_groups(node.left)
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)):
            lines.add(node.lineno)
    for cond in _conditions(tree):
        for n in ast.walk(cond):
            if _groups_access(n):
                lines.add(n.lineno)
    return sorted(lines)


def reads_through_crdecode(tree: ast.AST) -> bool:
    """Whether *tree* calls a da-crdecode reading of `_lib_consumer_read`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _READINGS:
            return True
        if isinstance(node, ast.Name) and node.id in _READINGS:
            return True
        if isinstance(node, ast.ImportFrom) and any(a.name in _READINGS for a in node.names):
            return True
    return False


def classifiers() -> "dict[str, list[int]]":
    out = {}
    for path in sorted(TOOLS.rglob("*.py")):
        if path == ENTRY:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lines = groups_decisions(tree)
        if lines and reads_through_crdecode(tree):
            out[path.relative_to(TOOLS).as_posix()] = lines
    return out


def test_no_tool_classifies_rule_files_from_a_reading_of_its_own():
    found = classifiers()
    new = {p: lines for p, lines in found.items() if p not in ALLOWED}
    assert not new, (
        "these modules decide from a da-crdecode reading whether something is a rule "
        "file (a `groups` key): ask `_lib_consumer_read.rule_files_in` instead "
        f"(#3426): {new}")


def test_every_allowed_module_still_matches():
    stale = sorted(set(ALLOWED) - set(classifiers()))
    assert not stale, f"no longer matched — delete them from ALLOWED: {stale}"


def test_the_search_finds_the_shapes_it_is_for():
    """Control: the shapes the classifiers this replaced were written in."""
    src = '''
import _lib_consumer_read
docs = _lib_consumer_read.any_yaml_documents("x")
if docs and isinstance(docs[0], dict) and "groups" in docs[0]:
    pass
ok = [d for d in docs if d.get("groups")]
'''
    tree = ast.parse(src)
    assert reads_through_crdecode(tree)
    assert groups_decisions(tree) == [4, 6]
    plain = ast.parse('rf = read()\nfor g in rf.documents[0]["groups"]:\n    pass\n')
    assert groups_decisions(plain) == [], "iterating groups decides nothing"
