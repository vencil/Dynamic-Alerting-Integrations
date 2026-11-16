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
        "x_liveness": {"databases": ["db"], "metrics": ["x_up"], "sql": "SELECT 1 AS x_up"},
        "x_main": {"databases": ["db"], "metrics": ["x_sessions", "x_waits"],
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

    def write(exprs, queries=None, records=None):
        (rp / "rule-pack-x.yaml").write_text(_pack(exprs, records), encoding="utf-8")
        (rp / "exporters" / "x.yaml").write_text(
            yaml.safe_dump(queries or QUERIES), encoding="utf-8")
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
