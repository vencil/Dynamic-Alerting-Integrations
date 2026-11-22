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
7. a metric the file declares and no query lists is never served (#4043): it
   is a finding by itself in both halves, and — with query-exporter installed
   — it is not derived as served, so a pack reading it gets the headline
   finding. A query whose `metrics` is not a list of names is "cannot run".
8. every metric a reference-validation waveform library of a query-exporter
   pack is written on (tests/rulepacks/reference-validation/) is one the pack's
   queries file serves (#4088): a signature on any other name can only MISS.
   A library that names no single pack, or no metric, is a failure — not a
   file the check stops reading.
9. a file whose shape the check cannot walk (#4119) — `metrics` / `queries`
   not a mapping, a metric or query that is not a mapping, a name that is not a
   string, a query's `metrics` / `databases` null or not a list of names — is
   rc 2 naming the file and the entry, without query-exporter too; every such
   shape is one query-exporter refuses as well (never stricter than it). What
   query-exporter refuses beyond that is rc 2 with its own location and reason.
10. the runtime half finds a counter's TYPE line under <name>_total (text format)
   or without `_total` (OpenMetrics), and an enum is exposed as a gauge or a
   stateset (#4119): a gauge served as a counter, and the reverse, are findings.
11. `interval` and `timeout` are read as query-exporter reads them (#4119): a
   malformed value is not a traceback, and "1e1" is a 10 s timeout.

The tests of 6, 7 and 8 that need query-exporter skip without it — and the
Python Tests job does not install it. They are not left to that: ci.yml's
Lint Rule Packs job installs query-exporter and runs this module with
QUERY_EXPORTER_REQUIRED=1, under which a missing package is a failure, not a
skip (`test_ci_runs_this_module_where_query_exporter_is_installed` holds that
step in place).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_exporter_queries as ceq  # noqa: E402

_REAL_SERVED_NAMES = ceq.served_names   # the `tree` fixture replaces it
_REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS = "tests/lint/test_check_exporter_queries.py"


def _need_query_exporter() -> None:
    """Skip without query-exporter — unless the caller said it must be there."""
    if os.environ.get("QUERY_EXPORTER_REQUIRED") == "1":
        import query_exporter  # noqa: F401,PLC0415  (a missing package fails the test)
        return
    pytest.importorskip("query_exporter")

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
    _need_query_exporter()
    q = tmp_path / "q.yaml"
    q.write_text(yaml.safe_dump(QUERIES), encoding="utf-8")
    names = ceq.served_names(q, QUERIES)
    assert {"x_up", "x_sessions", "x_waits_total", "x_waits_created"} <= names
    assert "x_waits" not in names


def _unattached(queries: dict) -> dict:
    """*queries* with x_sessions taken off the query that serves it (still declared)."""
    doc = yaml.safe_load(yaml.safe_dump(queries))
    doc["queries"]["x_main"]["metrics"] = ["x_waits"]
    doc["queries"]["x_main"]["sql"] = "SELECT 2 AS x_waits"
    return doc


def test_a_metric_no_query_lists_is_not_derived_as_served(tmp_path):
    """#4043: query-exporter serves a metric only from a query's result; one
    that is declared and attached to no query never appears on /metrics."""
    _need_query_exporter()
    doc = _unattached(QUERIES)
    q = tmp_path / "q.yaml"
    q.write_text(yaml.safe_dump(doc), encoding="utf-8")
    names = ceq.served_names(q, doc)
    assert "x_sessions" not in names
    assert {"x_up", "x_waits_total"} <= names
    # query-exporter's built-ins are listed by no query and served all the same.
    assert {"query_interval", "query_timestamp", "queries_total"} <= names


def test_a_declared_metric_no_query_lists_is_a_finding(tree):
    """Without query-exporter too: the declaration alone is the finding."""
    assert ceq.unattached_metrics(QUERIES) == set()
    doc = _unattached(QUERIES)
    assert ceq.unattached_metrics(doc) == {"x_sessions"}
    found = tree(["x_up == 0"], queries=doc)
    assert any("x_sessions is declared but no query lists it" in f for f in found), found


def test_a_pack_reading_an_unattached_metric_fails_with_the_real_derivation(tree, monkeypatch):
    """The headline of #4043, end to end with query-exporter's own code: the
    pack reads x_sessions, the file declares it, no query serves it."""
    _need_query_exporter()
    monkeypatch.setattr(ceq, "served_names", _REAL_SERVED_NAMES)
    found = tree(["x_sessions > 3"])
    assert found == [], found            # attached: served, nothing to say
    found = tree(["x_sessions > 3"], queries=_unattached(QUERIES))
    assert any("reads x_sessions, which" in f and "does not serve" in f for f in found), found


@pytest.mark.parametrize("listed", [[{"x_up": None}], 5, [["x_up"]], "x_up", None, []],
                         ids=["dict-item", "int", "nested-list", "string", "null", "empty"])
def test_a_query_whose_metrics_is_not_a_list_of_names_cannot_run(listed):
    doc = yaml.safe_load(yaml.safe_dump(QUERIES))
    doc["queries"]["x_main"]["metrics"] = listed
    with pytest.raises(ceq.CannotRun, match="x_main"):
        ceq.unattached_metrics(doc)


def _malformed(case: str) -> dict:
    """QUERIES with one shape the check cannot walk (#4119)."""
    doc = yaml.safe_load(yaml.safe_dump(QUERIES))
    if case == "query-null":
        doc["queries"]["x_main"] = None
    elif case == "query-string":
        doc["queries"]["x_main"] = "SELECT 1"
    elif case == "queries-list":
        doc["queries"] = list(doc["queries"].values())
    elif case == "metrics-list":
        doc["metrics"] = [{k: v} for k, v in doc["metrics"].items()]
    elif case == "metric-int-key":
        doc["metrics"][42] = {"type": "gauge", "expiration": "2m"}
    elif case == "metric-string":
        doc["metrics"]["x_sessions"] = "gauge"
    elif case == "query-metrics-null":
        doc["queries"]["x_main"]["metrics"] = None
    elif case == "query-databases-string":
        doc["queries"]["x_main"]["databases"] = "db"
    return doc


# (case, a word the refusal must carry: the entry, or the section)
MALFORMED = [("query-null", "x_main"), ("query-string", "x_main"),
             ("queries-list", "`queries`"), ("metrics-list", "`metrics`"),
             ("metric-int-key", "42"), ("metric-string", "x_sessions"),
             ("query-metrics-null", "x_main"), ("query-databases-string", "x_main")]


@pytest.mark.parametrize("case, named", MALFORMED, ids=[c for c, _ in MALFORMED])
def test_a_malformed_file_cannot_run_by_name(tree, monkeypatch, capsys, case, named):
    """#4119: rc 2 naming the file and the entry, not a traceback — and without
    query-exporter (the `tree` fixture stands in for its derivation)."""
    with pytest.raises(ceq.CannotRun, match=re.escape(named)):
        tree(["x_up == 0"], queries=_malformed(case))
    monkeypatch.setattr(sys, "argv", ["check_exporter_queries.py", "--ci"])
    assert ceq.main() == 2
    err = capsys.readouterr().err
    assert "rule-packs/exporters/x.yaml" in err and named in err, err


@pytest.mark.parametrize("case", [c for c, _ in MALFORMED])
def test_every_shape_refused_here_query_exporter_refuses(tmp_path, case):
    """shape_problem is a precondition of this tool's own walk, not a second
    schema: each shape it refuses, query-exporter's loader refuses too — an
    exporter that starts accepting one turns this red, and the check must follow."""
    _need_query_exporter()
    from query_exporter.config import load_config  # noqa: PLC0415
    db = tmp_path / "databases.yaml"
    db.write_text(yaml.safe_dump({"databases": {d: {"dsn": "sqlite://"}
                                                for d in ("db", "db_live")}}), encoding="utf-8")
    q = tmp_path / "q.yaml"
    q.write_text(yaml.safe_dump(_malformed(case)), encoding="utf-8")
    assert ceq.shape_problem(_malformed(case))
    # Refused = the loader does not return a config. Not pinned to ConfigError:
    # query-exporter 5.2.3 refuses a list `queries` / `metrics` by crashing in its
    # own merge (TypeError: unhashable type) — which is why this check walks the
    # shape itself rather than leaving it to the exporter.
    with pytest.raises(Exception):  # noqa: B017,PT011
        load_config([db, q])


def test_a_well_formed_file_has_no_shape_problem():
    assert ceq.shape_problem(QUERIES) is None
    assert ceq.shape_problem({}) is None          # absent sections: query-exporter's to judge


def test_query_exporter_refusal_names_where_and_why(tmp_path):
    """#4119: a refusal beyond the shapes above carries query-exporter's own
    location and reason, not only "1 validation error"."""
    _need_query_exporter()
    doc = yaml.safe_load(yaml.safe_dump(QUERIES))
    doc["queries"]["x_main"]["interval"] = "soon"
    q = tmp_path / "q.yaml"
    q.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ceq.CannotRun, match=r"queries\.x_main\.interval: .*interval"):
        ceq.served_names(q, doc)


def test_the_runtime_half_names_an_unattached_metric_too(tree, tmp_path):
    doc = _unattached(QUERIES)
    tree(["x_up == 0"], queries=doc)
    lines = ['queries_total{query="%s",status="success"} 1' % q for q in doc["queries"]]
    lines += ["x_up 1", "x_waits_total 1", "x_waits_created 1"]
    scrape = tmp_path / "scrape.txt"
    scrape.write_text("\n".join(lines) + "\n", encoding="utf-8")
    found = ceq.compare_served("x", scrape)
    assert any("x_sessions is declared but no query lists it" in f for f in found), found


def test_ci_runs_this_module_where_query_exporter_is_installed():
    """The Python Tests job has no query-exporter, so the tests above that need
    it skip there. Some job must install it and then run this module with
    QUERY_EXPORTER_REQUIRED=1 (a skip would be a failure), in that order."""
    ci = yaml.safe_load((_REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    for job in ci["jobs"].values():
        steps = job.get("steps") or []
        installs = [i for i, s in enumerate(steps)
                    if "pip install" in (s.get("run") or "") and "query-exporter" in (s.get("run") or "")]
        runs = [i for i, s in enumerate(steps)
                if "pytest" in (s.get("run") or "") and _THIS in (s.get("run") or "")
                and str((s.get("env") or {}).get("QUERY_EXPORTER_REQUIRED")) == "1"
                and "if" not in s and not s.get("continue-on-error")]
        if installs and runs and min(installs) < min(runs) and "if" not in job:
            return
    pytest.fail(f"no ci.yml job installs query-exporter and then runs {_THIS} "
                "with QUERY_EXPORTER_REQUIRED=1")


def test_required_means_a_missing_query_exporter_fails(monkeypatch):
    monkeypatch.setenv("QUERY_EXPORTER_REQUIRED", "1")
    monkeypatch.setitem(sys.modules, "query_exporter", None)
    with pytest.raises(ImportError):
        _need_query_exporter()


# Waveform libraries of packs whose exporter is NOT query-exporter: nothing here
# can say what those exporters serve (#3833 item 10 tracks their metric lists).
_NOT_QUERY_EXPORTER_LIBRARIES = {"k8s-linux-reference.yaml"}


def _waveform_libraries() -> dict[str, list[Path]]:
    """Reference-validation libraries by query-exporter pack: the pack's name
    is a `-`-separated word of the file name (oracle-reference-n2.yaml).

    Every file carrying `signatures` must land on exactly one pack or be named
    in _NOT_QUERY_EXPORTER_LIBRARIES — a renamed library is a failure here, not
    a file this check quietly stops reading."""
    packs = [pack for pack, _rule_pack, _queries in ceq.bindings()]
    found: dict[str, list[Path]] = {pack: [] for pack in packs}
    directory = _REPO_ROOT / "tests" / "rulepacks" / "reference-validation"
    unplaced = []
    for path in sorted(directory.iterdir()):
        if path.suffix not in (".yaml", ".yml"):
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "signatures" not in doc:
            continue
        owners = [pack for pack in packs if pack in path.stem.split("-")]
        if path.name in _NOT_QUERY_EXPORTER_LIBRARIES:
            assert not owners, f"{path.name} is listed as not query-exporter but names {owners}"
        elif len(owners) == 1:
            found[owners[0]].append(path)
        else:
            unplaced.append(f"{path.name} -> {owners}")
    assert not unplaced, f"waveform libraries that name no single pack: {unplaced}"
    return found


def waveform_metrics(doc: dict) -> set[str]:
    """Every series name a waveform library is written on, companions included."""
    names: set[str] = set()
    for signature in doc.get("signatures") or []:
        names.add(signature["metric"])
        names |= {c["metric"] for c in signature.get("companion_series") or []}
    return names


def test_waveform_libraries_are_written_on_served_metrics():
    """#4088: the hard-parse-storm signature sat on a parse-count metric the
    exporter never served, so the alert written for that incident could only
    MISS in the harness — and the miss read as a gap in the alert."""
    _need_query_exporter()
    libraries = _waveform_libraries()
    assert all(libraries.values()), f"a pack has no waveform library: {libraries}"
    problems = []
    for pack, _rule_pack, queries in ceq.bindings():
        served = _REAL_SERVED_NAMES(queries, ceq.load_queries(queries))
        for path in libraries[pack]:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            metrics = waveform_metrics(doc)
            assert metrics, f"{path.name} names no metric at all"
            for name in sorted(metrics - served):
                problems.append(f"{path.name}: {name} is not served by {queries.name}")
    assert not problems, "\n  ".join([""] + problems)


def test_waveform_metrics_reads_companions():
    doc = {"signatures": [{"metric": "a_total", "companion_series": [{"metric": "b_total"}]},
                          {"metric": "c"}]}
    assert waveform_metrics(doc) == {"a_total", "b_total", "c"}


def test_shipped_tree_is_clean():
    _need_query_exporter()
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


def test_compare_served_reads_a_counters_type_off_its_total_line(tree, tmp_path):
    """#4119: prometheus_client's text format writes a counter's TYPE line as
    <name>_total, so a lookup under <name> alone never compared a counter."""
    tree(["x_up == 0"])
    base = [
        "# TYPE x_up gauge", 'x_up{database="db"} 1.0',
        "# TYPE x_sessions gauge", 'x_sessions{database="db"} 3.0',
        "# TYPE x_waits_total counter", 'x_waits_total{database="db"} 2.0',
        "# TYPE x_waits_created gauge", 'x_waits_created{database="db"} 1.0',
        'queries_total{database="db",query="x_liveness",status="success"} 2.0',
        'queries_total{database="db",query="x_main",status="success"} 2.0',
    ]
    assert ceq.compare_served("x", _scrape(base, tmp_path)) == []
    # x_waits (a counter in the file) served as a gauge
    as_gauge = [line.replace("# TYPE x_waits_total counter", "# TYPE x_waits_total gauge")
                for line in base]
    found = ceq.compare_served("x", _scrape(as_gauge, tmp_path))
    assert any("x_waits is served as gauge, the file declares counter" in f for f in found), found
    # x_sessions (a gauge in the file) served as a counter
    as_counter = [line.replace("# TYPE x_sessions gauge", "# TYPE x_sessions_total counter")
                  for line in base]
    found = ceq.compare_served("x", _scrape(as_counter, tmp_path))
    assert any("x_sessions is served as counter, the file declares gauge" in f
               for f in found), found


@pytest.mark.parametrize("declared, lines, wrong", [
    # an enum: a gauge in the text format, a stateset in OpenMetrics
    ({"type": "enum", "states": ["a", "b"], "expiration": "1m"}, ["# TYPE st gauge"], False),
    ({"type": "enum", "states": ["a", "b"], "expiration": "1m"}, ["# TYPE st stateset"], False),
    ({"type": "enum", "states": ["a", "b"], "expiration": "1m"}, ["# TYPE st counter"], True),
], ids=["enum-text", "enum-openmetrics", "enum-as-counter"])
def test_compare_served_knows_how_each_type_is_exposed(tree, tmp_path, declared, lines, wrong):
    doc = yaml.safe_load(yaml.safe_dump(QUERIES))
    doc["metrics"]["st"] = declared
    doc["queries"]["x_main"]["metrics"].append("st")
    tree(["x_up == 0"], queries=doc)
    found = ceq.compare_served("x", _scrape(lines + ['st{st="a"} 1.0'], tmp_path))
    assert any("st is served as" in f for f in found) is wrong, found


def test_openmetrics_type_of_a_counter_declared_with_total():
    """OpenMetrics names a counter's TYPE line without `_total`, also when the
    file declares the counter as <x>_total."""
    assert ceq.served_type({"c": "gauge"}, "c_total") == "gauge"
    assert ceq.served_type({"c_total": "counter"}, "c") == "counter"
    assert ceq.served_type({"c": "counter"}, "c") == "counter"
    assert ceq.served_type({}, "c") is None


@pytest.mark.parametrize("field, value", [("timeout", "."), ("interval", "."),
                                          ("interval", "1.2.3"), ("timeout", [1])])
def test_a_malformed_interval_or_timeout_is_no_traceback(tree, field, value):
    """#4119: left to query-exporter's validation (which names its location),
    not a ValueError in this check's own parse."""
    doc = yaml.safe_load(yaml.safe_dump(QUERIES))
    doc["queries"]["x_main"][field] = value
    tree(["x_up == 0"], queries=doc)


def test_a_timeout_query_exporter_reads_as_a_float_is_compared():
    """"1e1" is a 10 s timeout to query-exporter; with a 5 s interval it is late."""
    assert ceq.timeout_seconds("1e1") == 10
    assert ceq.interval_seconds("5") == 5


_PARSE_VALUES = [15, 15.0, 1.5, "15", "2m", "1h", "1d", "15s", "1e1", "1.5", ".", "1.2.3",
                 " 15", "15 ", "15\n", "15s\n", "m", "", "-5", "0", 0.3, 0.15, True, None, [1], {"a": 1}]


@pytest.mark.parametrize("value", _PARSE_VALUES, ids=[repr(v) for v in _PARSE_VALUES])
def test_interval_and_timeout_parse_as_query_exporter_does(value):
    """interval_seconds / timeout_seconds against query-exporter's own field
    types (schema.TimeInterval, schema.Timeout): a value it accepts reads to
    the same number of seconds here. A value it refuses (by validation error or
    by its own crash) must not raise here — query-exporter then refuses the
    file in served_names, so what it reads as does not reach a verdict. A
    query-exporter that changes either field turns this red."""
    _need_query_exporter()
    from pydantic import TypeAdapter, ValidationError  # noqa: PLC0415
    from query_exporter import schema  # noqa: PLC0415
    for adapter, mine in ((TypeAdapter(schema.TimeInterval), ceq.interval_seconds),
                          (TypeAdapter(schema.Timeout), ceq.timeout_seconds)):
        got = mine(value)                      # never raises
        try:
            want = float(adapter.validate_python(value))
        except (ValidationError, TypeError):
            continue
        assert want == got, (value, want, got)


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
    q["queries"]["x_liveness"]["timeout"] = 15   # a float to query-exporter: "15s" it refuses
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
