"""Tests for check_exporter_queries.py (#3833).

Pinned contracts
----------------
1. a pack reading a name its queries file does not serve -> finding (headline).
2. recording rule outputs, the pack's own records and platform metrics
   (user_threshold, tenant_metadata_info) are not raw tenant metrics -> ignored.
3. a gauge without `expiration` -> finding; `increment: true` -> finding.
4. rate() / increase() of a served gauge -> finding; of a counter -> fine.
5. runtime half (--compare-served): a query that never succeeded, a query that
   failed, a declared metric that is not served -> findings; a built-in that
   appears only after an error (database_errors_total) absent -> fine.
6. with query-exporter installed: a counter is served as <name>_total and
   <name>_created (the derivation uses query-exporter's own code), and the
   shipped tree is clean.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_exporter_queries as ceq  # noqa: E402

QUERIES = {
    "metrics": {
        "x_up": {"type": "gauge", "expiration": "2m"},
        "x_sessions": {"type": "gauge", "expiration": "2m"},
        "x_waits": {"type": "counter"},
    },
    "queries": {
        "x_liveness": {"interval": 15, "timeout": 10, "databases": ["db_live"], "metrics": ["x_up"],
                       "sql": "SELECT 1 AS x_up"},
        "x_main": {"interval": 15, "timeout": 10, "databases": ["db"],
                   "metrics": ["x_sessions", "x_waits"],
                   "sql": "SELECT 1 AS x_sessions, 2 AS x_waits"},
    },
}
SERVED = {"x_up", "x_sessions", "x_waits_total", "x_waits_created",
          "query_interval", "query_timestamp", "queries_total", "queries_created"}


def _pack(exprs: list[str], records: dict[str, str] | None = None) -> str:
    rules = [{"record": r, "expr": e} for r, e in (records or {}).items()]
    rules += [{"alert": f"A{i}", "expr": e} for i, e in enumerate(exprs)]
    return yaml.safe_dump({"groups": [{"name": "g", "rules": rules}]})


@pytest.fixture
def tree(tmp_path, monkeypatch):
    rp = tmp_path / "rule-packs"
    (rp / "exporters").mkdir(parents=True)
    reg = rp / "threshold-registry.yaml"
    reg.write_text(yaml.safe_dump({"packs": {"x": {
        "rule_pack_file": "rule-packs/rule-pack-x.yaml",
        "queries": "rule-packs/exporters/x.yaml"}}}), encoding="utf-8")
    monkeypatch.setattr(ceq, "_REPO", tmp_path)
    monkeypatch.setattr(ceq, "_REGISTRY", reg)
    monkeypatch.setattr(ceq, "served_names", lambda path, doc: set(SERVED))
    monkeypatch.setattr(ceq, "_presence_selectors", lambda pack: [])

    def write(exprs, queries=None, records=None):
        (rp / "rule-pack-x.yaml").write_text(_pack(exprs, records), encoding="utf-8")
        (rp / "exporters" / "x.yaml").write_text(
            yaml.safe_dump(queries or QUERIES, sort_keys=False), encoding="utf-8")
        pack, rule_pack, q = ceq.bindings()[0]
        return ceq.findings_for(pack, rule_pack, q)
    return write


def test_clean_pack_has_no_findings(tree):
    assert tree(["x_up == 0", "rate(x_waits_total[5m]) > 1", "x_sessions > 3"]) == []


def test_unserved_name_is_a_finding(tree):
    found = tree(["x_sessions_typo > 3"])
    assert any("reads x_sessions_typo" in f for f in found), found


def test_records_and_platform_metrics_are_not_raw(tree):
    found = tree(
        ["tenant:x_sessions:max > on(tenant) group_left user_threshold{metric=\"s\"}",
         "x_up == 0 unless on(tenant) tenant_metadata_info"],
        records={"tenant:x_sessions:max": "max by(tenant) (x_sessions)"})
    assert found == [], found


def test_gauge_without_expiration_and_increment_are_findings(tree):
    q = yaml.safe_load(yaml.safe_dump(QUERIES))
    del q["metrics"]["x_sessions"]["expiration"]
    q["metrics"]["x_waits"]["increment"] = True
    found = tree(["x_up == 0"], queries=q)
    assert any("gauge x_sessions has no expiration" in f for f in found), found
    assert any("x_waits sets increment" in f for f in found), found


def test_rate_of_a_gauge_is_a_finding(tree):
    found = tree(["increase(x_sessions[5m]) > 1"])
    assert any("increase() to gauge x_sessions" in f for f in found), found


def _scrape(lines: list[str], tmp_path: Path) -> Path:
    p = tmp_path / "scrape.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_compare_served(tree, tmp_path):
    tree(["x_up == 0"])
    ok = [
        'x_up{database="db"} 1.0', 'x_sessions{database="db"} 3.0',
        'x_waits_total{database="db"} 2.0', 'x_waits_created{database="db"} 1.0',
        'queries_total{database="db",query="x_liveness",status="success"} 2.0',
        'queries_total{database="db",query="x_main",status="success"} 2.0',
    ]
    assert ceq.compare_served("x", _scrape(ok, tmp_path)) == []
    bad = [line for line in ok if not line.startswith("x_sessions")]
    bad = [line.replace('query="x_main",status="success"', 'query="x_main",status="error"')
           for line in bad]
    found = ceq.compare_served("x", _scrape(bad, tmp_path))
    assert any("query x_main has not succeeded once" in f for f in found), found
    assert any("query x_main failed 2 time(s)" in f for f in found), found
    assert any("x_sessions is derived" in f for f in found), found


def test_derivation_uses_query_exporter(tmp_path):
    pytest.importorskip("query_exporter")
    q = tmp_path / "q.yaml"
    q.write_text(yaml.safe_dump(QUERIES), encoding="utf-8")
    names = ceq.served_names(q, QUERIES)
    assert {"x_up", "x_sessions", "x_waits_total", "x_waits_created"} <= names
    assert "x_waits" not in names


def test_shipped_tree_is_clean():
    pytest.importorskip("query_exporter")
    for pack, rule_pack, queries in ceq.bindings():
        assert ceq.findings_for(pack, rule_pack, queries) == [], pack


def test_query_label_values_must_name_a_query(tree, monkeypatch):
    assert tree(['time() - query_timestamp{query="x_liveness"} > 60',
                 'query_interval{query=~"x_.+"}']) == []
    found = tree(['query_timestamp{query="x_alive"} > 0'])
    assert any('query="x_alive"' in f for f in found), found
    found = tree(['query_interval{query=~"y_.+"}'])
    assert any('query=~"y_.+"' in f for f in found), found
    monkeypatch.setattr(ceq, "_presence_selectors", lambda pack: ['query_interval{query="gone"}'])
    found = tree(["x_up == 0"])
    assert any("presence check" in f and 'query="gone"' in f for f in found), found


def test_compare_served_checks_types_and_the_builtins_the_pack_reads(tree, tmp_path):
    tree(['changes(query_timestamp{query="x_liveness"}[90s]) == 0'])
    base = [
        "# TYPE x_up gauge", 'x_up{database="db"} 1.0',
        "# TYPE x_sessions gauge", 'x_sessions{database="db"} 3.0',
        "# TYPE x_waits counter", 'x_waits_total{database="db"} 2.0',
        'x_waits_created{database="db"} 1.0',
        'queries_total{database="db",query="x_liveness",status="success"} 2.0',
        'queries_total{database="db",query="x_main",status="success"} 2.0',
        'query_timestamp{database="db",query="x_liveness"} 1.0',
    ]
    assert ceq.compare_served("x", _scrape(base, tmp_path)) == []
    no_ts = [line for line in base if not line.startswith("query_timestamp")]
    found = ceq.compare_served("x", _scrape(no_ts, tmp_path))
    assert any("reads query_timestamp" in f for f in found), found
    wrong_q = [line.replace('query="x_liveness"} 1.0', 'query="x_main"} 1.0') for line in base]
    found = ceq.compare_served("x", _scrape(wrong_q, tmp_path))
    assert any('query_timestamp{query="x_liveness"}' in f for f in found), found
    retyped = [line.replace("# TYPE x_sessions gauge", "# TYPE x_sessions counter") for line in base]
    found = ceq.compare_served("x", _scrape(retyped, tmp_path))
    assert any("x_sessions is served as counter" in f for f in found), found


@pytest.mark.parametrize("text, bad", [
    ("query_timestamp{query='x_gone'}", "x_gone"),                    # single quotes
    ('query_interval{query=~"x_liveness|x_gone"}', "x_gone"),         # alternation
    ('label_replace(v, "query", "x_gone", "", "")', "x_gone"),        # written by label_replace
])
def test_query_label_spellings(text, bad):
    found = ceq.query_label_findings([text], {"x_liveness", "x_main"}, "P")
    assert any(bad in f for f in found), found


def test_query_label_negative_and_regex_ok():
    assert ceq.query_label_findings(
        ['q{query=~"x_.+", query!="x_liveness"}', 'q{query="x_main"}'],
        {"x_liveness", "x_main"}, "P") == []


def test_query_label_regexes_do_not_backtrack():
    """A long run of backslashes with no closing quote must not hang (py/redos):
    with the overlapping body this took exponential time."""
    import time
    text = 'query="' + "\\" * 40 + 'x' + "\n" + 'label_replace(v, "query", "' + "\\" * 40 + "x"
    t0 = time.monotonic()
    ceq.query_label_findings([text], {"x_liveness"}, "P")
    assert time.monotonic() - t0 < 1.0


def test_escaped_quote_inside_query_value():
    found = ceq.query_label_findings(['q{query="x_\\"gone"}'], {"x_liveness"}, "P")
    assert any('x_"gone' in f for f in found), found


def test_backtick_query_selector(tree):
    found = tree(["query_timestamp{query=`x_gone`} > 0"])
    assert any('query="x_gone"' in f for f in found), found
    assert tree(["query_timestamp{query=`x_liveness`} > 0"]) == []


@pytest.mark.parametrize("expr", [
    "rate((x_sessions)[5m:]) > 1",
    "rate((sum(x_sessions))[5m:]) > 1",
    'increase(max by(db) (x_sessions{db=")"})[10m:1m]) > 1',
])
def test_rate_of_a_gauge_inside_its_argument(tree, expr):
    found = tree([expr])
    assert any("to gauge x_sessions" in f for f in found), found


def test_a_gauge_outside_the_rated_argument_is_fine(tree):
    assert tree(["rate(x_waits_total[5m]) / x_sessions > 1"]) == []


def test_query_without_timeout_or_with_a_late_one(tree):
    q = yaml.safe_load(yaml.safe_dump(QUERIES))
    del q["queries"]["x_main"]["timeout"]
    q["queries"]["x_liveness"]["timeout"] = "15s"
    found = tree(["x_up == 0"], queries=q)
    assert any("query x_main has no timeout" in f for f in found), found
    assert any("query x_liveness timeout 15s is not below its interval 15s" in f
               for f in found), found


def test_compare_served_expands_regex_selectors(tree, tmp_path):
    tree(['changes(query_timestamp{query=~"x_liveness|x_main"}[90s]) == 0'])
    base = [
        'x_up{database="db"} 1.0', 'x_sessions{database="db"} 3.0',
        'x_waits_total{database="db"} 2.0', 'x_waits_created{database="db"} 1.0',
        'queries_total{database="db",query="x_liveness",status="success"} 2.0',
        'queries_total{database="db",query="x_main",status="success"} 2.0',
        'query_timestamp{database="db",query="x_liveness"} 1.0',
        'query_timestamp{database="db",query="x_main"} 1.0',
    ]
    assert ceq.compare_served("x", _scrape(base, tmp_path)) == []
    one = [line for line in base if 'query_timestamp{database="db",query="x_main"}' not in line]
    found = ceq.compare_served("x", _scrape(one, tmp_path))
    assert any('query_timestamp{query="x_main"}' in f for f in found), found


def test_selected_queries_applies_every_matcher():
    qs = {"x_liveness", "x_main", "x_other"}
    assert ceq.selected_queries('query=~"x_.+", query!="x_liveness"', qs) == {"x_main", "x_other"}
    assert ceq.selected_queries("query!~'x_(main|other)'", qs) == {"x_liveness"}


def test_liveness_query_on_a_database_of_its_own(tree, monkeypatch):
    monkeypatch.setattr(ceq, "_presence_selectors",
                        lambda pack: ['query_interval{query="x_liveness"}'])
    q = yaml.safe_load(yaml.safe_dump(QUERIES))
    assert tree(["x_up == 0"], queries=q) == []
    q["queries"]["x_main"]["databases"] = ["db", "db_live"]
    found = tree(["x_up == 0"], queries=q)
    assert any("liveness query x_liveness shares database db_live with x_main" in f
               for f in found), found


def test_brace_inside_a_selector_regex():
    qs = {"x_liveness", "x_main"}
    text = 'query_timestamp{query=~"x_l{1}iveness"}'
    m = ceq._BUILTIN_SELECTOR.search(text)
    assert ceq.selected_queries(m.group(2), qs) == {"x_liveness"}


def test_label_replace_reading_query_is_not_a_write():
    assert ceq.query_label_findings(
        ['label_replace(x, "foo", "$1", "query", "(.*)")'], {"x_liveness"}, "P") == []
    found = ceq.query_label_findings(
        ['label_replace(x, "query", "x_gone", "", "")'], {"x_liveness"}, "P")
    assert any("x_gone" in f for f in found), found


@pytest.mark.parametrize("value", ["x_\\x6civeness", "x_\\u006civeness", "x_\\154iveness"])
def test_go_escapes_in_a_query_value_are_decoded(value):
    assert ceq.query_label_findings([f'q{{query="{value}"}}'], {"x_liveness"}, "P") == []


@pytest.mark.parametrize("value", ["oracle_café", "oracle_caf\\u00e9", "oracle_caf\\xc3\\xa9",
                                   "oracle_caf\\303\\251"])
def test_non_ascii_query_names_decode_by_go_rules(value):
    """Go: \\x / octal are bytes (UTF-8 assembled), \\u a code point, a literal
    é itself — all four spell the same name."""
    assert ceq.query_label_findings([f'q{{query="{value}"}}'], {"oracle_café"}, "P") == []
