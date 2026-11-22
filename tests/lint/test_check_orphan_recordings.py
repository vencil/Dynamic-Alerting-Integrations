"""Tests for scripts/tools/lint/check_orphan_recordings.py (TRK-339 WS2a / #1200).

The gate asserts every recording rule across the two rule trees has a consumer
(rule exprs / Grafana dashboards / observed map), grandfathering the records
already orphaned when it landed (#1204; 1 left after D-del). Each test pins one branch of that
contract:
  - the live repo is green (1 grandfathered + 1 exempt, 0 new drift)
  - a NEW orphan fails --ci
  - consumption is exact full-token identity — the substring/colon-adjacency
    trap (old-name-is-a-prefix-of-new-name) must NOT count as consumption
  - the KNOWN_ORPHANS exit-lock fires when a grandfathered record is rewired
    or deleted; EXEMPT_ORPHANS is existence-locked
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_orphan_recordings.py"

_spec = importlib.util.spec_from_file_location("check_orphan_recordings", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_has_no_new_drift():
    """Grandfathered orphans are INFO; zero NEW orphans and zero stale
    exemptions on the real artifacts."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


def test_grandfather_list_is_empty_and_nothing_is_orphaned():
    """#3778 deleted the last grandfathered orphan
    (tenant:mysql_connection_usage:ratio), so the list is empty and the real
    repo reports no orphan at all. Guards the list against regrowing."""
    result = gate.run_check()
    assert len(result["infos"]) == len(gate.KNOWN_ORPHANS) == 0
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


def test_sanity_anchor_mysql_connection_usage_percent_is_consumed():
    """#3778 anchor: the old orphan tenant:mysql_connection_usage:ratio is
    gone, and its replacement tenant:mysql_connection_usage:percent is a record
    the MariaDB connection alerts consume (so the scanner reaches it and does
    not report it)."""
    records = gate.collect_record_names()
    consumed = gate.collect_consumed_tokens()
    assert "tenant:mysql_connection_usage:ratio" not in records
    assert "tenant:mysql_connection_usage:percent" in records
    assert "tenant:mysql_connection_usage:percent" in consumed
    result = gate.run_check()
    assert not any("tenant:mysql_connection_usage:percent" in m
                   for m in result["infos"] + result["errors"])


def test_sanity_anchor_db2_lock_wait_time_is_not_orphan():
    """WS2a scan anchor: tenant:db2_lock_wait_time:rate5m is consumed by the
    DB2HighLockWaitTime alert and must never be reported."""
    records = gate.collect_record_names()
    consumed = gate.collect_consumed_tokens()
    assert "tenant:db2_lock_wait_time:rate5m" in records
    assert "tenant:db2_lock_wait_time:rate5m" in consumed
    result = gate.run_check()
    assert not any("tenant:db2_lock_wait_time:rate5m" in m
                   for m in result["errors"] + result["infos"])


# ── token identity: the substring / colon-adjacency trap ──────────────────

def _names(expr: str) -> set:
    """The names Prometheus' parse tree says *expr* reads (#4150)."""
    (names,) = gate._lib_consumer_read.promql_metric_names([expr])
    return set(names)


def test_a_record_name_is_one_name():
    """`tenant:alert_threshold:db2_lock_wait_time` is ONE name — a consumer
    of the threshold must not accidentally 'consume' the rate5m record whose
    name embeds the same substring."""
    names = _names("tenant:db2_lock_wait_time:rate5m > on(tenant) group_left "
                   "tenant:alert_threshold:db2_lock_wait_time")
    assert names == {"tenant:db2_lock_wait_time:rate5m", "tenant:alert_threshold:db2_lock_wait_time"}


def test_prefix_token_does_not_count_as_consumption():
    """A consumer referencing only the old/prefix name must leave the
    longer record orphaned (substring matching would hide this drift)."""
    result = gate.run_check(
        record_names={"tenant:db2_lock_wait_time:rate5m"},
        consumed=_names("tenant:alert_threshold:db2_lock_wait_time > 5"),
        known_orphans={},
        exempt={},
    )
    assert any("tenant:db2_lock_wait_time:rate5m" in e and "ORPHAN-RECORDING" in e
               for e in result["errors"]), result


@pytest.mark.parametrize("expr, want", [
    # functions, aggregations, keywords, label names and values are no names —
    # read off the parse, so no list of them to keep (#4150: holt_winters)
    ("rate(x[5m]) or absent(y) and sum by (tenant) (foo_metric) + vector(1)", {"x", "y", "foo_metric"}),
    ('label_replace(kube_pod_info, "tenant", `some_fake_metric`, "ns", `(.+)`)', {"kube_pod_info"}),
    ('{__name__="tenant:x:sum", tenant="db-a"} > 0', {"tenant:x:sum"}),
    ('x unless on(tenant) ClickHouseMetrics_TCPConnection', {"x", "ClickHouseMetrics_TCPConnection"}),
    ('{__name__=~"tenant:.*"} > 0', set()),
])
def test_the_names_an_expression_reads_are_its_parse_trees(expr, want):
    assert _names(expr) == want


@pytest.mark.parametrize("query, want", [
    # Grafana's interpolation, so the query parses (no name comes from a value)
    ("rate(x[$__rate_interval])", "rate(x[5m])"),
    ("x / $__interval_ms", "x / 300"),
    ("rate(x[${__interval}])", "rate(x[5m])"),
    ('sum(rate(x{tenant=~"$tenant"}[$window])) offset $off', 'sum(rate(x{tenant=~"x"}[5m])) offset 5m'),
    ("max_over_time(x[$range:$step])", "max_over_time(x[5m:1m])"),
    ("$metric{a=\"1\"}", "x{a=\"1\"}"),
    ("[[metric]] > 0", "x > 0"),
    ("label_values(user_threshold{metric=\"$metric\"}, severity)", "user_threshold{metric=\"x\"}"),
    ("query_result(topk(5, y))", "topk(5, y)"),
    ("label_values(tenant)", None),
    ("label_names()", None),
    ("metrics(tenant_.*)", None),
])
def test_grafana_queries_are_interpolated_as_grafana_does(query, want):
    assert gate.grafana_promql(query) == want


def _dashboards(tmp_path, panels):
    """A repo root holding one dashboard of *panels* and nothing else."""
    d = tmp_path / gate.DASHBOARD_DIRS[0]
    d.mkdir(parents=True)
    (d / "d.json").write_text(json.dumps({"panels": panels}), encoding="utf-8")
    return tmp_path


def test_a_dashboard_of_another_datasource_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "GRAFANA_CM_GLOB", ("nowhere", "*.yaml"))
    monkeypatch.setattr(gate, "EXTRA_DASHBOARDS", ())
    monkeypatch.setattr(gate, "iter_rules", lambda root: iter(()))
    monkeypatch.setattr(gate, "_walk_observed_series", lambda doc: iter(()))
    monkeypatch.setattr(gate, "_load_yaml", lambda path: {})
    root = _dashboards(tmp_path, [
        {"datasource": {"type": "loki", "uid": "l"}, "targets": [{"expr": '{app="x"} |= "error"'}]},
        {"datasource": {"type": "prometheus", "uid": "p"}, "targets": [{"expr": "rate(tenant:a:sum[5m])"}]},
        {"targets": [{"datasource": "${DS}", "expr": "tenant:b:max > 0"}]},
    ])
    assert gate.collect_consumed_tokens(root) == {"tenant:a:sum", "tenant:b:max"}


def test_a_dashboard_query_the_parser_refuses_is_a_caller_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "GRAFANA_CM_GLOB", ("nowhere", "*.yaml"))
    monkeypatch.setattr(gate, "EXTRA_DASHBOARDS", ())
    monkeypatch.setattr(gate, "iter_rules", lambda root: iter(()))
    root = _dashboards(tmp_path, [{"targets": [{"expr": "sum(rate(x[5m])"}]}])
    with pytest.raises(gate.DashboardQueryRefused) as exc:
        gate.collect_consumed_tokens(root)
    assert "d.json" in str(exc.value) and "sum(rate(x[5m])" in str(exc.value)


# ── new drift is caught / consumed records are clean ──────────────────────

def test_new_orphan_is_an_error():
    result = gate.run_check(
        record_names={"tenant:new_thing:rate5m"},
        consumed=set(),
        known_orphans={},
        exempt={},
    )
    assert any("tenant:new_thing:rate5m" in e and "ORPHAN-RECORDING" in e
               for e in result["errors"]), result


def test_consumed_record_is_clean():
    result = gate.run_check(
        record_names={"tenant:new_thing:rate5m"},
        consumed={"tenant:new_thing:rate5m"},
        known_orphans={},
        exempt={},
    )
    assert result["errors"] == [] and result["infos"] == []


def test_grandfathered_orphan_is_info_not_error():
    result = gate.run_check(
        record_names={"tenant:old_orphan:ratio"},
        consumed=set(),
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert result["errors"] == []
    assert any("tenant:old_orphan:ratio" in i for i in result["infos"])


# ── the exit-lock: the ledger must shrink as fixes land ───────────────────

def test_grandfathered_record_that_gained_a_consumer_is_stale_exemption():
    result = gate.run_check(
        record_names={"tenant:old_orphan:ratio"},
        consumed={"tenant:old_orphan:ratio"},   # rewired: now consumed
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert any("STALE-EXEMPTION" in e and "consumer" in e
               for e in result["errors"]), result


def test_grandfathered_record_that_was_deleted_is_stale_exemption():
    result = gate.run_check(
        record_names=set(),                     # record deleted
        consumed=set(),
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert any("STALE-EXEMPTION" in e and "no longer defined" in e
               for e in result["errors"]), result


def test_exempt_record_is_never_reported_while_it_exists():
    """custom_recipe_info-style inventory series: consumer-less by design."""
    result = gate.run_check(
        record_names={"custom_recipe_info"},
        consumed=set(),
        known_orphans={},
        exempt={"custom_recipe_info": "inventory"},
    )
    assert result["errors"] == [] and result["infos"] == []


def test_exempt_record_that_was_deleted_is_stale_exemption():
    result = gate.run_check(
        record_names=set(),
        consumed=set(),
        known_orphans={},
        exempt={"custom_recipe_info": "inventory"},
    )
    assert any("STALE-EXEMPTION" in e and "EXEMPT_ORPHANS" in e
               for e in result["errors"]), result


# ── the CLI exit-code contract (main), independent of real artifacts ──────

def test_main_without_ci_is_report_only(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["some drift"], "infos": []})
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["a breach"], "infos": []})
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": [], "infos": ["12 known-orphan"]})
    assert gate.main(["--ci"]) == gate.EXIT_OK
