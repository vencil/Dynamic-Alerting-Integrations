"""#2987: a policy rule whose `value` cannot serve its operator is refused
like any other rule the engine cannot read (#2961), on all four paths.

Every shape in `_REFUSED` used to evaluate anyway: a `when` that could never
hold skipped the rule, `not_equals` with no value always held, a dotted
wildcard target was looked up as a literal key and skipped (rc 0 / PASS),
and a main rule with no value / a bad regex / a scalar `one_of` reported
every tenant as violating. The four paths are evaluate-policy reading
`_defaults.yaml` or `--policy`, and validate-config reading `_defaults.yaml`
(the row FAILs, rc 1) or `--policy-dsl` (a caller error, rc 2).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops"))

import policy_engine as pe  # noqa: E402
import validate_config as vc  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


def _as_run(result):
    """What `_lib_exitcodes.run` does with main()'s answer (#3216): a Report's
    document is written and its status is the exit, as SystemExit."""
    raise SystemExit(emit_result(result))

_DEFAULTS = "defaults:\n  container_cpu: 80\n"
_TENANT = ("tenants:\n  tenant-a:\n    container_cpu: \"70\"\n"
           "    _metadata:\n      db_type: mariadb\n")
# A rule that holds for no tenant here (no tenant has an owner), so a rule
# guarded by a `when` that is actually evaluated fails the run.
_OWNER = "    target: _metadata.owner\n    operator: required\n"


def _rule(body: str) -> str:
    return "  - name: r\n" + body


# (id, rule, needle in the message). The needle names the field and why.
_REFUSED = [
    # Ticket rows that passed in silence.
    ("when-no-value", _rule(_OWNER + "    when: {target: container_cpu, operator: gte}\n"),
     "`when.operator: gte` compares against `when.value`, and none is given"),
    ("when-bad-regex",
     _rule(_OWNER + "    when: {target: _metadata.db_type, operator: matches, value: \"([a-z\"}\n"),
     "`when.value` '([a-z' is not a valid regular expression"),
    ("when-one_of-scalar",
     _rule(_OWNER + "    when: {target: _metadata.db_type, operator: one_of, value: mariadb}\n"),
     "`when.operator: one_of` needs `when.value` to be a non-empty list, got 'mariadb'"),
    ("when-wildcard-gte",
     _rule(_OWNER + "    when: {target: \"container_*\", operator: gte, value: 1}\n"),
     "a wildcard `when` supports only `required` / `forbidden`"),
    ("main-not_equals-no-value", _rule("    target: _metadata.db_type\n    operator: not_equals\n"),
     "`operator: not_equals` compares against `value`, and none is given"),
    ("main-dotted-wildcard",
     _rule("    target: \"_metadata.*\"\n    operator: equals\n    value: x\n"),
     "`target` '_metadata.*': a wildcard matches top-level keys only"),
    # Ticket rows reported as a violation of every tenant (misleading).
    ("main-gte-no-value", _rule("    target: container_cpu\n    operator: gte\n"),
     "`operator: gte` compares against `value`, and none is given"),
    ("main-matches-no-value", _rule("    target: _metadata.db_type\n    operator: matches\n"),
     "`operator: matches` compares against `value`, and none is given"),
    ("main-contains-no-value", _rule("    target: _metadata.db_type\n    operator: contains\n"),
     "`operator: contains` compares against `value`, and none is given"),
    ("main-bad-regex",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"([a-z\"\n"),
     "`value` '([a-z' is not a valid regular expression"),
    ("main-one_of-scalar",
     _rule("    target: _metadata.db_type\n    operator: one_of\n    value: mariadb\n"),
     "`operator: one_of` needs `value` to be a non-empty list"),
    # Same family, not on the ticket.
    ("when-dotted-wildcard", _rule(_OWNER + "    when: {target: \"_metadata.*\"}\n"),
     "`when.target` '_metadata.*': a wildcard matches top-level keys only"),
    ("presence-with-value", _rule("    target: container_cpu\n    value: 100\n"),
     "`operator: required` takes no `value`"),
    ("when-presence-with-value",
     _rule(_OWNER + "    when: {target: container_cpu, value: 100}\n"),
     "`when.operator: required` takes no `when.value`"),
    ("gte-not-a-number", _rule("    target: container_cpu\n    operator: gte\n    value: high\n"),
     "`operator: gte` needs `value` to be a number or a duration"),
    ("matches-redos",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"(a+)+\"\n"),
     "has a nested quantifier"),
    ("matches-nested-in-group",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"^((a+))+$\"\n"),
     "has a nested quantifier"),
    ("matches-brace-repeat",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"^(a+){2,}$\"\n"),
     "has a nested quantifier"),
    ("lte-infinite", _rule("    target: container_cpu\n    operator: lte\n    value: inf\n"),
     "`operator: lte` needs `value` to be a number or a duration"),
    ("gte-past-float-range",
     _rule("    target: container_cpu\n    operator: gte\n    value: " + "9" * 400 + "\n"),
     "`operator: gte` needs `value` to be a number or a duration"),
    ("matches-optional-group",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"^(\\\\d+)?$\"\n"),
     "an optional `(\\d+)?`"),
    ("one_of-empty", _rule("    target: _metadata.db_type\n    operator: one_of\n    value: []\n"),
     "needs `value` to be a non-empty list, got []"),
    ("equals-list", _rule("    target: _metadata.db_type\n    operator: equals\n    value: [a]\n"),
     "`operator: equals` needs `value` to be a scalar, got list"),
    ("matches-empty",
     _rule("    target: _metadata.db_type\n    operator: matches\n    value: \"\"\n"),
     "`value` is empty, which matches every value"),
    ("contains-empty",
     _rule("    target: _metadata.db_type\n    operator: contains\n    value: \"\"\n"),
     "`value` is empty, which every value contains"),
]

# The four paths: (id, where the rules go, expected rc).
_PATHS = ["evaluate-policy:_defaults.yaml", "evaluate-policy:--policy",
          "validate-config:_defaults.yaml", "validate-config:--policy-dsl"]


def _write_tree(tmp_path, policies_in_defaults: str = "") -> None:
    (tmp_path / "_defaults.yaml").write_text(_DEFAULTS + policies_in_defaults, encoding="utf-8")
    (tmp_path / "tenant-a.yaml").write_text(_TENANT, encoding="utf-8")


def _run(path, rule, tmp_path, capsys, cli_argv):
    """Run *rule* through *path*; return (rc, text naming the problems)."""
    in_defaults = path.endswith("_defaults.yaml")
    _write_tree(tmp_path, "_policies:\n" + rule if in_defaults else "")
    side = tmp_path.parent / f"{tmp_path.name}-policy.yaml"
    if not in_defaults:
        side.write_text("policies:\n" + rule, encoding="utf-8")
    if path.startswith("evaluate-policy"):
        argv = ["--config-dir", str(tmp_path), "--ci", "--json"]
        if not in_defaults:
            argv += ["--policy", str(side)]
        rc = emit_result(pe.main(argv))
        out = capsys.readouterr()
        doc = json.loads(out.out)
        return rc, doc, out.err
    argv = ["validate_config", "--config-dir", str(tmp_path), "--json"]
    if not in_defaults:
        argv += ["--policy-dsl", str(side)]
    cli_argv(*argv)
    with pytest.raises(SystemExit) as exc:
        _as_run(vc.main())
    row = {r["check"]: r for r in json.loads(capsys.readouterr().out)}["policy_dsl"]
    return exc.value.code, row, "\n".join(row["details"])


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("rule,needle", [(r, n) for _id, r, n in _REFUSED],
                         ids=[i for i, _r, _n in _REFUSED])
def test_rule_is_refused_on_every_path(rule, needle, path, tmp_path, capsys, cli_argv):
    rc, doc, text = _run(path, rule, tmp_path, capsys, cli_argv)
    if path.startswith("evaluate-policy"):
        assert (rc, doc["reason"], doc["passed"]) == (2, "policy_rule_invalid", False), doc
    elif path.endswith("_defaults.yaml"):
        assert (rc, doc["status"], doc["caller_error"]) == (1, "fail", False), doc
    else:
        assert (rc, doc["status"], doc["caller_error"]) == (2, "fail", True), doc
    assert "rule #0 ('r')" in text, text
    assert needle in text, text


# Each refused shape written so the engine can read it: the rule is evaluated
# and, on this tree, violated — the refusal above is not a rule that never runs.
_FIXED = [
    ("when-value", _rule(_OWNER + "    when: {target: container_cpu, operator: gte, value: 1}\n")),
    ("when-regex", _rule(_OWNER + "    when: {target: _metadata.db_type, operator: matches, value: \"^maria\"}\n")),
    ("when-one_of-list",
     _rule(_OWNER + "    when: {target: _metadata.db_type, operator: one_of, value: [mariadb]}\n")),
    ("when-wildcard-required", _rule(_OWNER + "    when: {target: \"container_*\", operator: required}\n")),
    ("not_equals-value",
     _rule("    target: _metadata.db_type\n    operator: not_equals\n    value: mariadb\n")),
    ("nested-field", _rule("    target: _metadata.db_type\n    operator: equals\n    value: x\n")),
    ("gte-value", _rule("    target: container_cpu\n    operator: gte\n    value: 100\n")),
    ("gte-duration-text", _rule("    target: container_cpu\n    operator: gte\n    value: \"5m\"\n")),
    ("one_of-list", _rule("    target: _metadata.db_type\n    operator: one_of\n    value: [mysql]\n")),
    ("contains-value", _rule("    target: _metadata.db_type\n    operator: contains\n    value: pg\n")),
]


@pytest.mark.usefixtures("da_guard_env")
@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("rule", [r for _id, r in _FIXED], ids=[i for i, _r in _FIXED])
def test_readable_rule_is_evaluated_on_every_path(rule, path, tmp_path, capsys, cli_argv):
    rc, doc, text = _run(path, rule, tmp_path, capsys, cli_argv)
    if path.startswith("evaluate-policy"):
        assert (rc, doc["passed"]) == (1, False), doc
        assert doc.get("reason") is None, doc
    else:
        assert (rc, doc["status"], doc["caller_error"]) == (1, "fail", False), doc
        assert "[ERROR] tenant-a: r" in text, text


@pytest.mark.parametrize("rule", [
    {"target": "x", "operator": "required", "value": None},   # `value:` left empty
    {"target": "x", "operator": "equals", "value": ""},
    {"target": "x", "operator": "equals", "value": False},
    {"target": "x", "operator": "lte", "value": "1.5"},
    {"target": "x", "operator": "lt", "value": 0},
    {"target": "x", "operator": "matches", "value": "^(ab{2})+$"},
    {"target": "x", "operator": "matches", "value": "^db-[0-9]+$"},
    {"target": "x", "operator": "matches", "value": 10},
    {"target": "x", "operator": "one_of", "value": ["a", 1, True]},
    {"target": "*_cpu", "operator": "lte", "value": 90},
    {"target": "x", "when": {"target": "*_cpu", "operator": "forbidden"}},
], ids=["presence-null-value", "equals-empty", "equals-bool", "numeric-text", "zero", "regex-fixed-count-repeat",
        "regex", "regex-number", "one_of-mixed", "wildcard-main-gte", "wildcard-when-forbidden"])
def test_readable_shapes_load(rule):
    """The checks refuse only what cannot be read: these load as written."""
    assert len(pe.rules_from_policy_data({"policies": [rule]})) == 1
