"""Tests for check_cluster_identity.py — rules keep `da_cluster` where they keep an identity (#4097).

Pinned contracts
----------------
1. the masking shape (headline): `a unless on(tenant, instance) b` with both
   sides exporter series is `on-drops-cluster`; with `da_cluster` in the
   matching it is clean.
2. `by (tenant…)` over exporter series is `by-drops-cluster`; with
   `da_cluster` it is clean; a grouping that names no identity label
   (`count(x)`, `sum by (pack)`) is a fleet-wide figure and is clean.
3. `without (da_cluster)` and `ignoring (da_cluster)` are findings.
4. threshold-exporter's series are one per tenant: joining exporter series
   to them with `on(tenant) group_left` is clean, and aggregating them by
   tenant is clean.
5. only the FIRST place the label is dropped is reported: what reads a
   result that already lost it is not a second finding.
6. a recorded series has the scope of the rule that records it, through a
   chain of rules.
7. `up` follows its `job`; a metric nobody classified is held to the contract.
8. the baseline is a two-way ratchet: a new finding fails, a healed one fails,
   `--generate` then check passes.
9. a per-cluster series joined to a one-per-tenant series `on (tenant)`
   must be the "many" side (`group_left`); without it Prometheus refuses the
   match as soon as there are two clusters.
10. two rules of one name with the same finding are counted, so fixing one
    changes the entry.
11. the shipped tree equals its baseline, and the scan read the real rule
    packs (the baseline is meant to end empty, so it cannot be the evidence).
"""
from __future__ import annotations

import json

import yaml
import os
import sys
from pathlib import Path

import pytest

_TOOLS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools")
sys.path.insert(0, os.path.join(_TOOLS, "lint"))
sys.path.insert(0, _TOOLS)

import _lib_consumer_read  # noqa: E402
import check_cluster_identity as cid  # noqa: E402

pytestmark = pytest.mark.usefixtures("da_read_env")


def found(expr: str, records: "dict[str, str] | None" = None) -> "list[tuple[str, str]]":
    out: "list[tuple[str, str]]" = []
    cid.judge(_lib_consumer_read.promql_shape([expr])[0], records or {}, out)
    return out


def scope(expr: str, records: "dict[str, str] | None" = None):
    return cid.judge(_lib_consumer_read.promql_shape([expr])[0], records or {}, [])


def test_masking_join_is_the_headline_finding():
    masked = ("max by (tenant, da_cluster, instance) (db_expected) "
              "unless on (tenant, instance) max by (tenant, da_cluster, instance) (db_up == 1)")
    assert found(masked) == [(cid.ON_DROPS, "tenant,instance")]
    fixed = masked.replace("on (tenant, instance)", "on (tenant, da_cluster, instance)")
    assert found(fixed) == []


@pytest.mark.parametrize("expr, want", [
    ("sum by (tenant) (rate(db_deadlocks_total[5m]))", [(cid.BY_DROPS, "tenant")]),
    ("max by (tenant, instance) (db_up)", [(cid.BY_DROPS, "tenant,instance")]),
    ("max by (namespace, pod) (kube_pod_status_ready)", [(cid.BY_DROPS, "namespace,pod")]),
    ("sum by (tenant, da_cluster) (rate(db_deadlocks_total[5m]))", []),
    ("count(db_up)", []),
    ("sum by (pack) (db_up)", []),
    ("topk by (tenant) (3, db_up)", [(cid.BY_DROPS, "tenant")]),
    # a selecting aggregation returns its input's series, labels and all: the
    # aggregation around it is where the label goes
    ("sum by (tenant) (topk(3, db_up))", [(cid.BY_DROPS, "tenant")]),
    ("max by (tenant) (bottomk(1, db_up))", [(cid.BY_DROPS, "tenant")]),
    ("sum by (tenant, da_cluster) (topk(3, db_up))", []),
    ("sum by (tenant) (topk by (tenant) (3, db_up))",
     [(cid.BY_DROPS, "tenant"), (cid.BY_DROPS, "tenant")]),
    # a date function over a vector keeps the vector's labels
    ("sum by (tenant) (month(db_last_backup_timestamp))", [(cid.BY_DROPS, "tenant")]),
    ("sum by (tenant) (db_up * month())", [(cid.BY_DROPS, "tenant")]),
])
def test_grouping(expr, want):
    assert found(expr) == want


def test_without_and_ignoring_the_cluster_label():
    assert found("sum without (da_cluster) (db_up)") == [(cid.WITHOUT_CLUSTER, "da_cluster")]
    assert found("sum without (pod) (db_up)") == []
    assert found("db_up and ignoring (da_cluster) db_ok") == [(cid.IGNORING_CLUSTER, "da_cluster")]
    assert found("db_up and ignoring (pod) db_ok") == []


def test_tenant_scoped_platform_series_join_on_tenant():
    expr = ('max by (tenant, da_cluster) (db_sessions) > on (tenant) group_left '
            'max by (tenant) (user_threshold{metric="sessions"})')
    assert found(expr) == []
    assert scope(expr) == cid.CLUSTER
    assert found('max by (tenant) (user_state_filter{filter="maintenance"})') == []
    assert scope("tenant_metadata_info") == cid.FLAT


def test_only_the_first_drop_is_reported():
    expr = ("(max by (tenant) (db_a)) unless on (tenant) (max by (tenant) (db_b))")
    assert found(expr) == [(cid.BY_DROPS, "tenant"), (cid.BY_DROPS, "tenant")]


def test_recorded_series_take_their_rules_scope():
    assert found("tenant:x:max > on (tenant) group_left tenant:alert_threshold:x",
                 {"tenant:x:max": cid.FLAT, "tenant:alert_threshold:x": cid.FLAT}) == []
    # `{__name__="x"}` is the same series as `x`: the parser leaves the name in the matcher
    assert found('sum by (tenant) ({__name__="tenant:x:max"})', {"tenant:x:max": cid.FLAT}) == []
    assert found('sum by (tenant) ({__name__="db_up"})') == [(cid.BY_DROPS, "tenant")]
    assert found('sum by (tenant) ({__name__=~"tenant:x:.*"})', {"tenant:x:max": cid.FLAT}) == [
        (cid.BY_DROPS, "tenant")]      # a pattern is nobody's name: held to the contract
    both = {"a:b": cid.CLUSTER, "c:d": cid.CLUSTER}
    assert found("a:b unless on (tenant, instance) c:d", both) == [(cid.ON_DROPS, "tenant,instance")]


def test_up_follows_its_job_and_the_unclassified_are_held():
    assert scope('up{job="tenant-exporters"}') == cid.CLUSTER
    assert scope('up{job="threshold-exporter"}') == cid.FLAT
    assert scope("up") == cid.CLUSTER
    # a job nobody listed is an exporter's, not the platform's
    assert scope('up{job="mysqld-exporter"}') == cid.CLUSTER
    assert scope('scrape_duration_seconds{job="mysqld-exporter"}') == cid.CLUSTER
    assert scope('scrape_duration_seconds{job="prometheus"}') == cid.FLAT
    assert scope("upstream_requests_total") == cid.CLUSTER and not cid.is_per_target("upstream_x")
    assert scope("some_metric_nobody_listed") == cid.CLUSTER
    assert scope("prometheus_rule_evaluation_failures_total") == cid.FLAT
    assert scope("kube_pod_info") == cid.CLUSTER
    assert scope('ALERTS{alertstate="firing"}') == cid.FLAT
    assert scope("absent(db_up)") == cid.FLAT


@pytest.mark.parametrize("label", sorted(cid.IDENTITY_LABELS))
def test_every_identity_label_is_one(label):
    assert found(f"max by ({label}) (db_up)") == [(cid.BY_DROPS, label)]
    assert found(f"db_up unless on ({label}) db_ok") == [(cid.ON_DROPS, label)]


@pytest.mark.parametrize("expr, want", [
    # set operators: the left side's series come out
    ("db_up and on (tenant) user_state_filter", cid.CLUSTER),
    ("user_state_filter unless on (tenant) db_up", cid.FLAT),
    ("user_state_filter and on (tenant) db_up", cid.FLAT),
    ("user_state_filter or db_up", cid.CLUSTER),
    ("db_up or user_state_filter", cid.CLUSTER),
    ("user_state_filter or tenant_metadata_info", cid.FLAT),
    # a scalar on one side: the vector's scope
    ("db_up > 5", cid.CLUSTER), ("5 < db_up", cid.CLUSTER), ("user_threshold > 5", cid.FLAT),
    # the "many" side's series come out
    ("db_up * on (tenant) group_left user_threshold", cid.CLUSTER),
    ("user_threshold * on (tenant) group_right db_up", cid.CLUSTER),
    ("user_threshold * on (tenant) group_left () tenant_metadata_info", cid.FLAT),
    # one-to-one on(...) keeps only the matched labels
    ("db_up / on (tenant, da_cluster, instance) db_total", cid.CLUSTER),
    ("db_up / db_total", cid.CLUSTER),
    ("db_up / on (tenant) db_total", cid.FLAT),
    ("db_up / on (tenant) user_threshold", cid.FLAT),
    ("histogram_quantile(0.9, db_bucket)", cid.CLUSTER),
    # calls and the rest
    ("rate(db_x_total[5m])", cid.CLUSTER),
    ("clamp_min(user_threshold, 0)", cid.FLAT),
    ('label_replace(db_up, "a", "$1", "b", "(.*)")', cid.CLUSTER),
    ("max_over_time(db_up[5m:1m])", cid.CLUSTER),
    ("-db_up", cid.CLUSTER),
    ("sum without (pod) (db_up)", cid.CLUSTER),
    ("sum without (pod) (user_threshold)", cid.FLAT),
    ("absent(db_up)", cid.FLAT),
    ("scalar(db_up) * user_threshold", cid.FLAT),
    ("topk(3, db_up)", cid.CLUSTER), ("bottomk(2, db_up)", cid.CLUSTER),
    ("topk without (da_cluster) (3, db_up)", cid.CLUSTER),
    ("sum(db_up)", cid.FLAT),
    ("month(db_ts)", cid.CLUSTER), ("day_of_week(user_threshold)", cid.FLAT),
    ("month()", cid.FLAT), ("day_of_week()", cid.FLAT),   # vector(time()): no labels, a vector
    ("time()", None), ("pi()", None),
    ("vector(1)", cid.FLAT),
    ('label_replace(vector(1), "tenant", "t", "__name__", ".*")', cid.FLAT),
])
def test_scope_of_each_shape(expr, want):
    assert scope(expr) == want


def test_what_is_under_a_node_is_judged_too():
    # an aggregation's parameter, a call's argument, a scalar comparison's vector
    assert found("topk(scalar(max by (tenant) (db_up)), db_other)") == [(cid.BY_DROPS, "tenant")]
    assert found("clamp_min(max by (tenant) (db_up), 0)") == [(cid.BY_DROPS, "tenant")]
    assert found("5 < max by (tenant) (db_up)") == [(cid.BY_DROPS, "tenant")]
    assert found("sum by (tenant, da_cluster) (sum without (pod) (db_up))") == []
    assert found("sum by (tenant) (sum without (pod) (db_up))") == [(cid.BY_DROPS, "tenant")]
    # ignoring / on with one flat side are not the both-sides findings
    assert found("db_up and ignoring (da_cluster) user_state_filter") == []
    assert found("db_up unless on (tenant) user_state_filter") == []


@pytest.mark.parametrize("expr, want", [
    ("max by (tenant, da_cluster) (db_x) > on (tenant) max by (tenant) (user_threshold)",
     [(cid.ONE_SIDE, "tenant")]),
    ("max by (tenant) (user_threshold) < on (tenant) group_left max by (tenant, da_cluster) (db_x)",
     [(cid.ONE_SIDE, "tenant")]),
    ("max by (tenant, da_cluster) (db_x) > on (tenant) group_left max by (tenant) (user_threshold)", []),
    ("max by (tenant) (user_threshold) < on (tenant) group_right max by (tenant, da_cluster) (db_x)", []),
    ("max by (tenant, da_cluster) (db_x) and on (tenant) user_state_filter", []),
    ("count by (da_cluster) (db_x) > on () group_left count(user_threshold)", []),
    ('max by (tenant, da_cluster) (db_x) > on (tenant) label_replace(vector(1), "tenant", "t", "", "")',
     [(cid.ONE_SIDE, "tenant")]),
    ('max by (tenant, da_cluster) (db_x) > on (tenant) label_replace(month(), "tenant", "t", "", "")',
     [(cid.ONE_SIDE, "tenant")]),
    ('label_replace(month(), "tenant", "t", "", "") * on (tenant) group_right max by (tenant, da_cluster) (db_x)',
     []),
])
def test_a_per_cluster_side_must_be_the_many_side(expr, want):
    assert found(expr) == want


def _good_tree() -> dict:
    return _lib_consumer_read.promql_shape(
        ["sum by (tenant) (rate(x_total[5m])) > on (tenant) group_left topk(1, y)"])[0]


@pytest.mark.parametrize("break_it", [
    lambda t: t.pop("matching"),                                  # a key its kind must carry
    lambda t: t.__setitem__("kind", "something-new"),             # a kind this reader does not know
    lambda t: t.__setitem__("start", "0"),                        # an offset that is no number
    lambda t: t["matching"].__setitem__("labels", "tenant"),      # labels that are no list
    lambda t: t["lhs"].__setitem__("without", "no"),              # `without` that is no bool
    lambda t: t["lhs"]["expr"].__setitem__("args", None),         # a call without arguments list
    lambda t: t["lhs"]["expr"]["args"][0].__setitem__("range", 1),  # a selector's range flag
    lambda t: t["rhs"].__setitem__("param", "1"),                 # a parameter that is no node
    lambda t: t["lhs"].__setitem__("expr", {"kind": "paren", "start": 0, "end": 1}),  # a child cut short
    lambda t: t.__setitem__("kind", []),                          # a kind that is no string
    lambda t: t["lhs"]["expr"].__setitem__("name", []),           # a call whose name is no string
    lambda t: t.__setitem__("op", 1),                             # an operator that is no string
    lambda t: t.__setitem__("bool", "false"),                     # `bool` that is no bool
    lambda t: t["lhs"].__setitem__("labels", ["tenant", 1]),      # a label that is no string
    lambda t: t["matching"].__setitem__("include", [None]),       # a carried label that is no string
    lambda t: t["lhs"]["expr"]["args"][0].__setitem__("matchers", [{"name": "a"}]),  # a matcher cut short
], ids=["missing-key", "unknown-kind", "offset", "labels", "without", "args", "range",
        "param", "truncated-child", "kind-type", "name-type", "op-type", "bool-type",
        "label-type", "include-type", "matcher"])
def test_a_tree_of_another_shape_is_not_taken(break_it, monkeypatch):
    """A da-read older or newer than this reader prints another shape: that
    is "could not read" by name, never a tree with a hole a check would read
    as "no grouping here"."""
    assert _lib_consumer_read._is_shape(_good_tree())
    bad = _good_tree()
    break_it(bad)
    assert not _lib_consumer_read._is_shape(bad)
    monkeypatch.setattr(_lib_consumer_read, "_run", lambda *a, **k: {"documents": [[bad]]})
    with pytest.raises(_lib_consumer_read.ConsumerReadError, match="one tree per expression"):
        _lib_consumer_read.promql_shape(["x"])


@pytest.mark.parametrize("answer", [{}, {"documents": []}, {"documents": [[]]},
                                    {"documents": [[], []]}, {"documents": "x"}])
def test_an_answer_without_one_tree_per_expression_is_not_taken(answer, monkeypatch):
    monkeypatch.setattr(_lib_consumer_read, "_run", lambda *a, **k: answer)
    with pytest.raises(_lib_consumer_read.ConsumerReadError, match="one tree per expression"):
        _lib_consumer_read.promql_shape(["x"])


def test_an_unparsable_expression_is_refused_by_name():
    with pytest.raises(_lib_consumer_read.InputRefused):
        _lib_consumer_read.promql_shape(["sum by ("])


def _tree(tmp_path: Path, rules: "list[tuple[str, str]]") -> Path:
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    body = "\n".join(
        f"      - {'record' if ':' in n else 'alert'}: {n}\n        expr: {json.dumps(e)}"
        for n, e in rules)
    (packs / "rule-pack-x.yaml").write_text(f"groups:\n  - name: g\n    rules:\n{body}\n",
                                            encoding="utf-8")
    return tmp_path


def test_scopes_settle_through_a_chain_of_recording_rules(tmp_path):
    root = _tree(tmp_path, [
        ("XDown", "tenant:x:c unless on (tenant, instance) tenant:x:b"),
        ("tenant:x:c", "tenant:x:b * 2"),
        ("tenant:x:b", "tenant:x:a + 0"),
        ("tenant:x:a", "max by (tenant, da_cluster, instance) (x_up)"),
    ])
    assert cid.findings(root) == {"rule-pack-x.yaml": ["g / XDown: on-drops-cluster (tenant,instance)"]}


def test_a_chain_that_ends_flat_is_flat_all_the_way(tmp_path):
    """The default for a name not settled yet is cluster; a chain whose root
    is flat has to be propagated to be read right, in whatever order the
    rules are written."""
    root = _tree(tmp_path, [
        ("XHigh", "max by (tenant) (tenant:x:c)"),
        ("tenant:x:c", "tenant:x:b * 2"),
        ("tenant:x:b", "tenant:x:a + 0"),
        ("tenant:x:a", "max by (tenant) (user_threshold)"),
    ])
    assert cid.findings(root) == {}


def test_a_record_without_a_colon_is_still_a_record(tmp_path):
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    (packs / "rule-pack-x.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n"
        "      - record: plain_info\n        expr: max by (tenant) (user_threshold)\n"
        "      - alert: XHigh\n        expr: max by (tenant) (plain_info) > 0\n", encoding="utf-8")
    assert cid.findings(tmp_path) == {}


def test_a_name_recorded_with_two_scopes_is_held_to_the_contract(tmp_path):
    for order in ((0, 1), (1, 0)):
        root = tmp_path / f"o{order[0]}"
        root.mkdir()
        recs = [("tenant:x:a", "max by (tenant) (user_threshold)"),
                ("tenant:x:a", "max by (tenant, da_cluster) (x_up)")]
        _tree(root, [recs[order[0]], recs[order[1]], ("XHigh", "max by (tenant) (tenant:x:a)")])
        assert cid.findings(root) == {
            "rule-pack-x.yaml": ["g / XHigh: by-drops-cluster (tenant)"]}, order


def test_same_named_rules_are_counted_together(tmp_path, monkeypatch):
    root = _tree(tmp_path, [("XDown", "max by (tenant) (x_up) == 0"),
                            ("XDown", "max by (tenant) (x_up) == 0")])
    assert cid.findings(root) == {"rule-pack-x.yaml": ["g / XDown: by-drops-cluster (tenant) ×2"]}
    monkeypatch.setattr(cid, "PROJECT_ROOT", root)
    monkeypatch.setattr(cid, "BASELINE", root / "baseline.yaml")
    assert cid.generate() == cid.EXIT_OK and cid.check() == cid.EXIT_OK
    _tree_rewrite = (root / "rule-packs" / "rule-pack-x.yaml")
    _tree_rewrite.write_text(_tree_rewrite.read_text(encoding="utf-8").replace(
        "max by (tenant) (x_up) == 0", "max by (tenant, da_cluster) (x_up) == 0", 1), encoding="utf-8")
    assert cid.check() == cid.EXIT_VIOLATION


def test_repeats_in_one_rule_are_counted(tmp_path):
    root = _tree(tmp_path, [("XDown", "max by (tenant) (x_a) + on (tenant) max by (tenant) (x_b)")])
    assert cid.findings(root) == {"rule-pack-x.yaml": ["g / XDown: by-drops-cluster (tenant) ×2"]}


@pytest.mark.parametrize("expr, want", [
    ("max by (tenant, da_cluster) (x_up) > on (tenant, da_cluster) group_left user_threshold",
     [("on-cluster-with-flat-side", "tenant,da_cluster")]),
    ("max by (tenant, da_cluster) (x_up) unless on (tenant, da_cluster) (user_state_filter == 1)",
     [("on-cluster-with-flat-side", "tenant,da_cluster")]),
    ("max by (tenant, da_cluster) (x_up) > on (tenant) group_left user_threshold", []),
    ("max by (tenant, da_cluster) (x_a) and on (tenant, da_cluster) max by (tenant, da_cluster) (x_b)", []),
    ("user_threshold and on (tenant, da_cluster) user_state_filter", []),
], ids=["threshold-join", "maintenance-unless", "on-tenant-is-right", "both-per-cluster", "both-flat"])
def test_a_per_tenant_series_is_not_matched_on_the_cluster_label(expr, want):
    found = []
    cid.judge(_lib_consumer_read.promql_shape([expr])[0], {}, found)
    assert found == want


def _declared_tree(tmp_path: Path, comment: str, expr: str, between: str = "") -> Path:
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    (packs / "rule-pack-x.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n"
        f"      {comment}\n{between}      - alert: XNoPrimary\n        expr: {json.dumps(expr)}\n",
        encoding="utf-8")
    return tmp_path


_TENANT_WIDE = "count by (tenant, da_cluster) (x_a) and on (tenant) count by (tenant) (x_b)"


def test_a_declared_tenant_wide_read_is_not_a_finding(tmp_path):
    root = _declared_tree(
        tmp_path, "# da_cluster: tenant-wide by-drops-cluster (tenant) — asked of all clusters on purpose",
        _TENANT_WIDE)
    assert cid.findings(root) == {} and cid.stale_declarations(root) == []
    # … and without the comment the same rule is one
    plain = _tree(tmp_path / "plain", [("XNoPrimary", _TENANT_WIDE)]) if (tmp_path / "plain").mkdir() is None else None
    assert cid.findings(plain) == {"rule-pack-x.yaml": ["g / XNoPrimary: by-drops-cluster (tenant)"]}


def test_a_declaration_excuses_only_the_finding_it_names(tmp_path):
    root = _declared_tree(
        tmp_path, "# da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose",
        "max by (tenant, instance) (x_a) and on (tenant) count by (tenant) (x_b)")
    assert cid.findings(root) == {
        "rule-pack-x.yaml": ["g / XNoPrimary: by-drops-cluster (tenant,instance)"]}


def test_a_declaration_is_counted_once(tmp_path):
    root = _declared_tree(
        tmp_path, "# da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose",
        "count by (tenant) (x_a) and on (tenant) count by (tenant) (x_b)")
    assert cid.findings(root) == {"rule-pack-x.yaml": ["g / XNoPrimary: by-drops-cluster (tenant)"]}


@pytest.mark.parametrize("comment, expr, between, says", [
    ("# da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose",
     "count by (tenant, da_cluster) (x_a)", "", "has 0 such finding"),
    ("# da_cluster: tenant-wide by-drops-cluster (tenant)", _TENANT_WIDE, "", "needs a reason"),
    ("# da_cluster: tenant-wide by-drops-cluster (tenant) —", _TENANT_WIDE, "", "needs a reason"),
    ("# da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose", _TENANT_WIDE,
     "      - alert: Other\n        expr: vector(1)\n", "has 0 such finding"),
], ids=["nothing-to-excuse", "no-reason", "empty-reason", "above-another-rule"])
def test_a_declaration_that_excuses_nothing_fails(tmp_path, monkeypatch, capsys, comment, expr, between, says):
    root = _declared_tree(tmp_path, comment, expr, between)
    assert any(says in x for x in cid.stale_declarations(root))
    monkeypatch.setattr(cid, "PROJECT_ROOT", root)
    monkeypatch.setattr(cid, "BASELINE", root / "baseline.yaml")
    (root / "baseline.yaml").write_text(
        yaml.safe_dump({"findings": cid.findings(root)}), encoding="utf-8")
    assert cid.check() == cid.EXIT_VIOLATION
    assert "excuse nothing" in capsys.readouterr().out


def test_a_declaration_belongs_to_the_rule_below_it_not_to_its_namesake(tmp_path):
    """Two rules of one name (warning, critical): the declaration above the
    first does not excuse the second."""
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    (packs / "rule-pack-x.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n"
        "      # da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose\n"
        "      - alert: X\n        expr: max by (tenant, da_cluster) (x_up) == 0\n"
        "      - alert: X\n        expr: max by (tenant) (x_up) == 0\n", encoding="utf-8")
    assert cid.findings(tmp_path) == {"rule-pack-x.yaml": ["g / X: by-drops-cluster (tenant)"]}
    assert any("has 0 such finding" in x for x in cid.stale_declarations(tmp_path))


@pytest.mark.parametrize("comment, says", [
    ("# da_cluster: tenant-wide on-cluster-with-flat-side (tenant,da_cluster) — on purpose",
     "cannot be declared"),
    ("# da_cluster: tenant-wide one-side-needs-group (tenant) — on purpose", "cannot be declared"),
    ("# da_cluster: tenant-wide by-drops-cluster tenant — on purpose", "not read as a declaration"),
    ("# da_cluster : tenant-wide by-drops-cluster (tenant) — on purpose", "not read as a declaration"),
], ids=["a-rule-that-does-not-work", "needs-group", "no-parentheses", "stray-space"])
def test_a_declaration_that_is_not_one_is_reported_and_excuses_nothing(tmp_path, comment, says):
    root = _declared_tree(tmp_path, comment, _TENANT_WIDE)
    assert any(says in x for x in cid.stale_declarations(root))
    assert cid.findings(root) == {"rule-pack-x.yaml": ["g / XNoPrimary: by-drops-cluster (tenant)"]}


def test_a_declaration_not_directly_above_a_rule_fails(tmp_path):
    packs = tmp_path / "rule-packs"
    packs.mkdir()
    (packs / "rule-pack-x.yaml").write_text(
        "groups:\n  - name: g\n    # da_cluster: tenant-wide by-drops-cluster (tenant) — on purpose\n"
        "    rules:\n      - alert: XNoPrimary\n"
        f"        expr: {json.dumps(_TENANT_WIDE)}\n", encoding="utf-8")
    assert any("directly above its rule" in x for x in cid.stale_declarations(tmp_path))
    assert cid.findings(tmp_path) == {"rule-pack-x.yaml": ["g / XNoPrimary: by-drops-cluster (tenant)"]}


def test_baseline_is_a_two_way_ratchet(tmp_path, monkeypatch, capsys):
    root = _tree(tmp_path, [("XDown", "max by (tenant) (x_up) == 0")])
    monkeypatch.setattr(cid, "PROJECT_ROOT", root)
    monkeypatch.setattr(cid, "BASELINE", root / "baseline.yaml")
    assert cid.check() == cid.EXIT_VIOLATION
    assert "+ rule-pack-x.yaml: g / XDown: by-drops-cluster (tenant)" in capsys.readouterr().out
    assert cid.generate() == cid.EXIT_OK and cid.check() == cid.EXIT_OK
    capsys.readouterr()
    (root / "rule-packs" / "rule-pack-x.yaml").write_text(
        "groups:\n  - name: g\n    rules:\n      - alert: XDown\n"
        "        expr: max by (tenant, da_cluster) (x_up) == 0\n", encoding="utf-8")
    assert cid.check() == cid.EXIT_VIOLATION
    assert "- rule-pack-x.yaml: g / XDown: by-drops-cluster (tenant)" in capsys.readouterr().out


def test_generate_that_cannot_write_the_baseline_is_exit_2(tmp_path, monkeypatch, capsys):
    """A baseline that cannot be written is "could not run" (2), never a
    traceback and never a green `--generate` that left the old list."""
    root = _tree(tmp_path, [("XDown", "max by (tenant) (x_up) == 0")])
    blocked = root / "baseline.yaml"
    blocked.mkdir()                       # a directory where the file goes
    monkeypatch.setattr(cid, "PROJECT_ROOT", root)
    monkeypatch.setattr(cid, "BASELINE", blocked)
    monkeypatch.setattr(sys, "argv", ["check_cluster_identity.py", "--generate"])
    assert cid.main() == cid.EXIT_CANNOT_RUN
    assert "cannot run" in capsys.readouterr().err
    assert blocked.is_dir()


def test_an_empty_tree_cannot_run(tmp_path):
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "rule-packs" / "rule-pack-x.yaml").write_text("groups: []\n", encoding="utf-8")
    with pytest.raises(cid.CannotRun):
        cid.findings(tmp_path)


def test_shipped_tree_equals_its_baseline_and_the_scan_is_not_vacuous():
    """The baseline is meant to end empty (#4097), so "the list holds rule X"
    cannot be what shows the check looked at the real tree: the number of
    rules it read is."""
    assert cid.check() == cid.EXIT_OK
    rules = cid._rules(cid.PROJECT_ROOT)
    assert len(rules) > 300, f"only {len(rules)} rules read from the rule packs"
    assert {r[0] for r in rules} >= {"rule-pack-db2.yaml", "rule-pack-kubernetes.yaml",
                                     "rule-pack-liveness.yaml", "rule-pack-platform.yaml"}


def test_every_job_the_shipped_rules_read_up_under_is_classified():
    """`up` under a job this check does not know would be read as an
    exporter's by default; the shipped tree must say which it is."""
    jobs = cid.per_target_jobs()
    assert "tenant-exporters" in jobs, "the scan found no tenant-exporter `up`: vacuous"
    unknown = {j for j in jobs if j is not None} - cid.PLATFORM_JOBS - cid.CLUSTER_JOBS
    assert not unknown, f"add to PLATFORM_JOBS or CLUSTER_JOBS on purpose: {sorted(unknown)}"
    assert not cid.PLATFORM_JOBS & cid.CLUSTER_JOBS
