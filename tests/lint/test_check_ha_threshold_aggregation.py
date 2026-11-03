"""Tests for check_ha_threshold_aggregation.py — HA-max invariant lint.

Pinned contracts
----------------
1. **Detection**: `sum`/`avg`/`min`/etc applied directly to `user_threshold`
   is flagged; the offending operator is returned.
2. **max is HA-safe**: `max by(...) (user_threshold...)` → no finding.
3. **Operator-only (Gemini adversarial review)**: the `by(...)` grouping is
   NOT constrained — `max by(tenant, version, env)` passes (dimensions
   preserved); the lint only judges the aggregation operator.
4. **Scope**: only aggregations OF `user_threshold` — real-metric
   aggregation (`sum(rate(mysql_...))`) is never flagged.
5. **Live dogfood**: every committed rule pack / operator / configmap copy
   already aggregates user_threshold with max (gates a regression of the
   ADR-024 PR3-pre HA fix).
6. **Reached, not adjacent (#3483)**: the expr is parsed, so an aggregation
   that reaches raw `user_threshold` through a function, parentheses or a
   binary operator is judged like a direct one; a `max` in between collapses
   the replica copies and the outer aggregation passes.
7. **Fail closed**: an expr `promql-parser` cannot parse is an error
   (`UnparseableExpr`, main rc 2), never a pass.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_ha_threshold_aggregation as lint  # noqa: E402
import _lib_consumer_read as cr  # noqa: E402  (#3471: the rule-source declaration)


def _ops(expr: str):
    return [op for op, _excerpt in lint.nonmax_aggregations(expr)]


def test_sum_flagged():
    assert _ops('sum by(tenant) (user_threshold{metric="redis_memory", severity="warning"})') == ["sum"]


def test_max_clean():
    assert _ops('max by(tenant) (user_threshold{metric="redis_memory", severity="warning"})') == []


def test_avg_and_min_flagged():
    assert _ops('avg by(tenant) (user_threshold{metric="x"})') == ["avg"]
    assert _ops('min by(tenant) (user_threshold{metric="x"})') == ["min"]


def test_max_with_extra_dimensions_clean():
    # Gemini point: dimensions in by() must NOT cause a flag — operator only.
    assert _ops('max by(tenant, version, severity) (user_threshold{metric="cpu"})') == []
    assert _ops('max by(tenant, env) (user_threshold{metric="cpu", env="prod"})') == []


def test_sum_with_dimensions_still_flagged():
    # sum is the HA danger regardless of grouping.
    assert _ops('sum by(tenant, version) (user_threshold{metric="cpu"})') == ["sum"]


def test_inline_comment_does_not_hide_sum():
    # Gemini adversarial review: a `#` line comment between `(` and
    # user_threshold must NOT swallow the token and hide the sum (false neg).
    expr = 'sum by(tenant) (\n  # threshold value\n  user_threshold{metric="x"}\n)'
    assert _ops(expr) == ["sum"]


def test_commented_out_sum_not_flagged():
    # A fully commented-out sum line is not a live aggregation.
    expr = ('# sum by(tenant) (user_threshold{metric="x"})\n'
            'max by(tenant) (user_threshold{metric="x"})')
    assert _ops(expr) == []


def test_real_metric_aggregation_not_flagged():
    # Real metric aggregation is legitimate; only user_threshold is in scope.
    assert _ops('sum by(namespace, pod) (rate(container_cpu_usage_seconds_total[5m]))') == []
    assert _ops('sum by(tenant) (rate(mysql_connections[5m]))') == []


def test_no_user_threshold_clean():
    assert _ops('max by(tenant) (tenant:alert_threshold:foo)') == []


def test_whitespace_variants_flagged():
    # Token-adjacency whitespace must not hide a sum.
    assert _ops('sum by (tenant) ( user_threshold{metric="x"} )') == ["sum"]
    assert _ops('sum(user_threshold{metric="x"})') == ["sum"]


# --- 6. reached through functions / parens / binary operators (#3483) -------

@pytest.mark.parametrize("expr, ops", [
    # The CustomAlertReplicasDisagree shape with its inner max removed: the
    # outer count then counts replica copies (scrape-identity labels differ).
    ('count by (tenant, recipe_id) (label_replace(user_threshold{component="custom"},'
     ' "threshold_severity", "$1", "severity", "(.*)"))', ["count"]),
    ('sum(abs(user_threshold{metric="x"}))', ["sum"]),
    ('sum(user_threshold{metric="x"} * 2)', ["sum"]),
    ('sum(2 * user_threshold{metric="x"})', ["sum"]),
    ('topk(1, (user_threshold{metric="x"}))', ["topk"]),
    ('sum(max_over_time(user_threshold{metric="x"}[5m]))', ["sum"]),
    ('sum(real_metric or user_threshold{metric="x"})', ["sum"]),
    ('sum({__name__="user_threshold", metric="x"})', ["sum"]),
    ('count by (tenant) (\n'
     '  label_replace(\n'
     '    # a comment between the aggregation and the selector\n'
     '    clamp_min(user_threshold{metric="x"}, 0),\n'
     '    "a", "$1", "b", "(.*)"\n'
     '  )\n'
     ') > 1', ["count"]),
], ids=["label_replace", "abs", "times-2", "2-times", "topk-paren",
        "over_time", "or-rhs", "__name__", "multi-line"])
def test_aggregation_reaching_user_threshold_is_flagged(expr, ops):
    assert _ops(expr) == ops


@pytest.mark.parametrize("expr", [
    # The committed CustomAlertReplicasDisagree shape: max collapses the copies.
    'count by (tenant, recipe_id, threshold_severity) (label_replace('
    'max by (tenant, recipe_id, severity, name, mode) (user_threshold{component="custom"}),'
    ' "threshold_severity", "$1", "severity", "(.*)")) > 1',
    'max by (tenant) (user_threshold{metric="x"})',
    'user_threshold{metric="x"} > 3',
    'sum(rate(real_metric[5m]))',
    'sum(real_metric and on(tenant) user_threshold{metric="x"})',
    'count(absent(user_threshold{metric="x"}))',
    'sum by (tenant) (real_metric) > on(tenant) max by (tenant) (user_threshold{metric="x"})',
], ids=["max-inside-count", "max", "no-aggregation", "real-metric",
        "and-filter-rhs", "absent", "compare-to-max"])
def test_copies_collapsed_or_absent_pass(expr):
    assert _ops(expr) == []


def test_a_hash_inside_a_string_is_not_a_comment():
    # The regex version stripped `#...` to end-of-line, string or not, so the
    # `sum` after it on the same line vanished; the parser keeps a `#` inside
    # a string literal as text.
    expr = ('max by (a) (label_replace(real_metric, "a", "#", "b", "(.*)"))'
            ' + sum(user_threshold{metric="x"})')
    assert _ops(expr) == ["sum"]


# One test per propagation / selection rule in the module doc, so reverting
# any single rule turns exactly its test red.

def test_unless_rhs_only_filters():
    assert _ops('sum(real_metric unless user_threshold{metric="x"})') == []
    assert _ops('sum(user_threshold{metric="x"} unless real_metric)') == ["sum"]


@pytest.mark.parametrize("expr", [
    'sum(scalar(user_threshold{metric="x"}) * real_metric)',
    'count(absent_over_time(user_threshold{metric="x"}[5m]))',
], ids=["scalar", "absent_over_time"])
def test_non_carrying_functions(expr):
    assert _ops(expr) == []


def test_subquery_passes_copies_through():
    assert _ops('sum(max_over_time(user_threshold{metric="x"}[5m:1m]))') == ["sum"]


def test_aggregation_param_is_walked():
    # quantile's φ is itself an expression; a sum inside it is judged too.
    assert _ops('quantile(scalar(sum(user_threshold{metric="x"})), real_metric)') == ["sum"]


@pytest.mark.parametrize("expr, ops", [
    ('sum({__name__=~"user_.*"})', ["sum"]),
    ('sum({__name__=~"real_.*", tenant="x"})', []),
    ('sum({__name__!~"user_.*", tenant="x"})', []),
    ('sum({__name__="a" or __name__="user_threshold"})', ["sum"]),
    ('sum({__name__="a" or __name__="b"})', []),
], ids=["re-accepts", "re-rejects", "not-re-rejects", "or-second", "or-neither"])
def test_name_matchers(expr, ops):
    assert _ops(expr) == ops


@pytest.mark.parametrize("expr", [
    'sum({__name__=~"user_\\\\pL+"})',          # RE2 `\pL`: Python re.error
    'sum({__name__=~"user_[[:alpha:]]+"})',     # POSIX class: Python reads another set
    'sum({__name__!~"user_\\\\pL+", tenant="x"})',
], ids=["re-error", "posix-class", "not-re-error"])
def test_undecidable_name_regex_fails_closed(expr):
    assert _ops(expr) == ["sum"]


@pytest.mark.parametrize("expr", [
    'sum({tenant="x"})',
    'sum({__name__!="foo", tenant="x"})',
    'sum({__name__!~"foo.*", tenant="x"})',
], ids=["nameless", "name-not-equal", "name-not-re"])
def test_nameless_selector_may_select_user_threshold(expr):
    # Prometheus would select user_threshold with these; no textual mention.
    assert _ops(expr) == ["sum"]


@pytest.mark.parametrize("expr", [
    'sum(user_threshold_x{tenant="x"})',
    'sum(user_threshold:foo)',
    'sum({__name__!="user_threshold", tenant="x"})',
], ids=["prefix-name", "recording-rule-name", "name-excluded"])
def test_other_metric_names_pass(expr):
    # Exact name, not a prefix (the regex version flagged the first two).
    assert _ops(expr) == []


def test_unknown_node_kind_raises():
    with pytest.raises(lint.UnparseableExpr, match="unhandled PromQL node"):
        lint._walk(object(), [])


def test_parser_panic_is_an_error_not_a_crash(monkeypatch):
    class PanicException(BaseException):  # pyo3's panic type is a BaseException
        pass

    def boom(_s):
        raise PanicException("rust panicked")

    monkeypatch.setattr(lint.promql_parser, "parse", boom)
    with pytest.raises(lint.UnparseableExpr, match="PanicException"):
        lint.nonmax_aggregations("sum(user_threshold)")


# --- 7. fail closed ---------------------------------------------------------

def test_unparseable_expr_raises_not_passes():
    with pytest.raises(lint.UnparseableExpr):
        lint.nonmax_aggregations('sum(user_threshold{metric="x"}')


def test_unparseable_expr_names_the_rule():
    groups = [{"name": "g", "rules": [{"record": "r:x", "expr": "sum(user_threshold"}]}]
    with pytest.raises(lint.UnparseableExpr, match="r:x"):
        lint.check_groups(groups)


_RAW = """\
groups:
  - name: t-threshold-normalization
    interval: 15s
    rules:
      - record: tenant:alert_threshold:x
        expr: {op} by(tenant) (user_threshold{{metric="x", severity="warning"}})
"""
_CRD = """\
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: da-rule-pack-t
spec:
  groups:
    - name: t-threshold-normalization
      rules:
        - record: tenant:alert_threshold:x
          expr: {op} by(tenant) (user_threshold{{metric="x"}})
"""
_CM = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-rules-t
data:
  t-recording.yml: |
    groups:
      - name: t-threshold-normalization
        rules:
          - record: tenant:alert_threshold:x
            expr: {op} by(tenant) (user_threshold{{metric="x"}})
"""


def _findings(path):
    return lint.findings_by_file([path])[str(path)]


@pytest.mark.parametrize("template", [_RAW, _CRD, _CM], ids=["raw", "crd", "configmap"])
def test_check_file_all_three_formats(tmp_path, template):
    """_iter_rule_groups must parse raw rule-pack / CRD / ConfigMap alike."""
    sum_file = tmp_path / "sum.yaml"
    sum_file.write_text(template.format(op="sum"), encoding="utf-8")
    findings = _findings(sum_file)
    assert len(findings) == 1 and findings[0][1] == "sum"

    max_file = tmp_path / "max.yaml"
    max_file.write_text(template.format(op="max"), encoding="utf-8")
    assert _findings(max_file) == []


def test_a_file_prometheus_refuses_is_named_not_skipped(tmp_path):
    """#3206: a YAML scalar document is no rule file Prometheus loads; it is
    refused by name (it used to be skipped as "no groups"). #3426: it is no
    manifest either, so `rule_files_in` names it with its readers' reason."""
    p = tmp_path / "scalar.yaml"
    p.write_text("just a string\n", encoding="utf-8")
    with pytest.raises(lint._lib_consumer_read.RuleFileSourcesRefused) as exc_info:
        _findings(p)
    assert [path for path, _why in exc_info.value.files] == [str(p)]


def test_main_names_a_refused_file_rc2(tmp_path, monkeypatch, capsys):
    for d in ("rule-packs", "k8s/03-monitoring"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "rule-packs" / "rule-pack-t.yaml").write_text(
        "groups:\n  - name: g\n    rulez: []\n", encoding="utf-8")
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_ha_threshold_aggregation.py", "--ci"])
    assert lint.main() == 2
    err = capsys.readouterr().err
    assert "rule-pack-t.yaml" in err and "field rulez not found" in err


@pytest.mark.parametrize("where", ["rule-packs/rule-pack-t.yaml",
                                   "k8s/03-monitoring/configmap-rules-t.yaml"])
def test_main_judges_a_file_by_its_readers_not_its_directory(tmp_path, monkeypatch, where):
    """#3426 F7: main reads every target through `rule_files_in`. A
    ConfigMap under rule-packs/ (or a raw pack among the manifests) used to
    go to the wrong reader by its directory: refused, or read as no rules."""
    for d in ("rule-packs", "k8s/03-monitoring"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / where).write_text(_CM.format(op="sum"), encoding="utf-8")
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_ha_threshold_aggregation.py", "--ci"])
    assert lint.main() == 1
    (tmp_path / where).write_text(_RAW.format(op="sum"), encoding="utf-8")
    assert lint.main() == 1


def test_main_exit_codes(tmp_path, monkeypatch):
    """main(--ci) returns 1 on a violation, 0 when clean."""
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "k8s" / "03-monitoring").mkdir(parents=True)
    pack = tmp_path / "rule-packs" / "rule-pack-t.yaml"
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)

    pack.write_text(_RAW.format(op="sum"), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["check_ha_threshold_aggregation.py", "--ci"])
    assert lint.main() == 1

    pack.write_text(_RAW.format(op="max"), encoding="utf-8")
    assert lint.main() == 0


def _ha_tree(tmp_path, monkeypatch):
    for d in ("rule-packs", "k8s/03-monitoring"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(lint, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["check_ha_threshold_aggregation.py", "--ci"])
    return tmp_path / "rule-packs"


def test_main_reads_a_yml_rule_pack(tmp_path, monkeypatch):
    """#3426: rule-packs/ is read through `rule_pack_files` — a `.yml` pack
    is a pack (the lint's own `glob("rule-pack-*.yaml")` did not see it)."""
    rp = _ha_tree(tmp_path, monkeypatch)
    (rp / "rule-pack-t.yml").write_text(_RAW.format(op="sum"), encoding="utf-8")
    assert lint.main() == 1


def test_main_names_an_undeclared_yaml_in_rule_packs(tmp_path, monkeypatch, capsys):
    """#3426: a YAML in rule-packs/ that is neither a pack nor declared
    beside them is named, rc 2 — it used to be passed over in silence."""
    rp = _ha_tree(tmp_path, monkeypatch)
    (rp / "rule-pack-t.yaml").write_text(_RAW.format(op="max"), encoding="utf-8")
    (rp / "extra.yaml").write_text(_RAW.format(op="sum"), encoding="utf-8")
    assert lint.main() == 2
    assert "extra.yaml" in capsys.readouterr().err


def test_live_repo_all_max():
    """Dogfood: every committed copy aggregates user_threshold with max."""
    repo = Path(__file__).resolve().parents[2]
    # #3471: every declared rule file of the declared families.
    targets = [p for s in cr.RULE_SOURCES for p in s.files_under(repo)]
    offenders = {}
    for path in targets:
        findings = _findings(path)
        if findings:
            offenders[str(path.relative_to(repo))] = findings
    assert not offenders, f"non-max user_threshold aggregation(s): {offenders}"


def test_main_without_parser_is_rc2_not_a_pass(tmp_path, monkeypatch, capsys):
    """Fail closed: when the expressions cannot be parsed (here: no parser),
    main exits 2 naming the rule — it never reports `use max`."""
    rp = _ha_tree(tmp_path, monkeypatch)
    (rp / "rule-pack-t.yaml").write_text(_RAW.format(op="max"), encoding="utf-8")
    monkeypatch.setattr(lint, "promql_parser", None)
    assert lint.main() == 2
    assert "tenant:alert_threshold:x" in capsys.readouterr().err


_INNER_MAX = ('max by (tenant, recipe_id, severity, name, mode) '
              '(user_threshold{component="custom"})')


def test_live_replica_disagreement_alert_without_inner_max_is_caught(
        tmp_path, monkeypatch, capsys):
    """#3483: the committed CustomAlertReplicasDisagree with its inner `max`
    replaced by the raw selector makes the outer `count` count replica
    copies. The regex version passed that (rc 0); it must be rc 1."""
    repo = Path(__file__).resolve().parents[2]
    text = (repo / "rule-packs" / "rule-pack-platform.yaml").read_text(encoding="utf-8")
    assert text.count(_INNER_MAX) == 1, "the alert's inner max changed — update this test"
    rp = _ha_tree(tmp_path, monkeypatch)
    (rp / "rule-pack-platform.yaml").write_text(
        text.replace(_INNER_MAX, 'user_threshold{component="custom"}'), encoding="utf-8")
    assert lint.main() == 1
    out = capsys.readouterr().out
    assert "CustomAlertReplicasDisagree" in out and "`count`" in out
