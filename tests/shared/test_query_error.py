"""``_lib_prometheus.QueryError`` and the range / Alertmanager readers (#3133).

A query a tool asked for and did not get a usable answer to used to come back
as an empty result — ``(None, err)`` collapsed into ``[]`` by the caller, or a
``success`` body of the wrong shape read as "no rows" by the lib itself — and
was reported as "no data" with a meaningful-looking exit code. The first batch
of #3133 (#3073, #3074) moves the range query and the Alertmanager alert list
to readers that raise ``QueryError`` (a named ``CallerError``: rc 2, one
``ERROR:`` line, the ``--json`` envelope), with the shape check in one place.

Three parts:

* the shape check (``_matrix_rows`` / ``alertmanager_alerts``) — every
  defect named, the good shapes kept;
* ``QueryError`` reaching ``_lib_exitcodes.run`` uncaught: rc 2 and an
  envelope naming ``<source>_query_error`` (a subprocess: the exit path is
  only observable at the end of a real interpreter);
* the static gate: no tool defines its own ``*QueryError`` again, and the
  set of tools still on the tuple helpers / raw ``urlopen`` only shrinks.
"""
from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from _tree import REPO_ROOT, repo_files

import _lib_exitcodes
import _lib_prometheus as lp

TOOLS_DIR = REPO_ROOT / "scripts" / "tools"


# ── shape check ──────────────────────────────────────────────────────

def _ok(result, result_type="matrix"):
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


_ROW = {"metric": {"tenant": "t"}, "values": [[1, "1"], [2, "2"]]}


@pytest.mark.parametrize("body", [
    _ok([]),
    _ok([_ROW]),
    _ok([{"values": []}]),  # no metric, no samples: still a matrix row
    _ok([_ROW, {"metric": {}, "histograms": [[1, {"count": "3", "sum": "1"}]]}]),
    _ok([dict(_ROW, histograms=[[1, {"count": "1"}]])]),
], ids=["empty-matrix", "one-row", "row-without-metric", "histogram-only-row",
        "values-and-histograms"])
def test_matrix_rows_accepts_a_matrix(body):
    assert lp._matrix_rows(body) == body["data"]["result"]


@pytest.mark.parametrize("body,defect", [
    (None, "body is not a JSON object"),
    ([], "body is not a JSON object"),
    ({"status": "success"}, "'data' is missing or not an object"),
    ({"status": "success", "data": "x"}, "'data' is missing or not an object"),
    ({"status": "success", "data": {"result": []}}, "resultType is None"),
    (_ok([], "vector"), "resultType is 'vector'"),
    (_ok(None), "'data.result' is missing or not a list"),
    (_ok({"a": 1}), "'data.result' is missing or not a list"),
    (_ok([_ROW, "x"]), "result[1] is not an object"),
    (_ok([{"metric": "x", "values": []}]), "result[0].metric is not an object"),
    (_ok([_ROW, {"metric": {}}]), "result[1] has neither 'values' nor 'histograms'"),
    (_ok([{"values": None}]), "result[0].values is not a list"),
    (_ok([{"histograms": "x"}]), "result[0].histograms is not a list"),
    (_ok([{"values": [], "histograms": [[1]]}]), "result[0].histograms holds [1]"),
    (_ok([{"values": [[1]]}]), "result[0].values holds [1]"),
    (_ok([{"values": ["x"]}]), "result[0].values holds 'x'"),
], ids=["null", "array", "no-data", "data-string", "no-resultType", "vector",
        "result-null", "result-object", "row-string", "metric-string",
        "row-without-values", "values-null", "histograms-string", "histogram-sample-short",
        "sample-short", "sample-string"])
def test_matrix_rows_names_the_defect(body, defect):
    with pytest.raises(lp.QueryError) as exc:
        lp._matrix_rows(body)
    assert str(exc.value).startswith("malformed Prometheus response: "), exc.value
    assert defect in str(exc.value), exc.value
    assert (exc.value.source, exc.value.reason) == ("Prometheus", "prometheus_query_error")


@pytest.mark.parametrize("raw,want", [
    ("1", 1.0), ("-2.5", -2.5), ("1e3", 1000.0), ("+Inf", math.inf), ("-Inf", -math.inf),
])
def test_sample_value_reads_prometheus_samples(raw, want):
    assert lp.sample_value(raw) == want


def test_sample_value_nan():
    assert math.isnan(lp.sample_value("NaN"))


@pytest.mark.parametrize("raw", ["abc", "", 1, 1.5, None, [1]])
def test_sample_value_refuses_what_is_not_a_sample_string(raw):
    """#3213: the API writes every sample as a string; a number or garbage is
    not an answer, refused at the one reading."""
    with pytest.raises(ValueError):
        lp.sample_value(raw)


@pytest.mark.parametrize("value", ["abc", 1, None])
def test_matrix_rows_names_an_unreadable_sample(value):
    body = _ok([_ROW, {"metric": {}, "values": [[1, "1"], [2, value]]}])
    with pytest.raises(lp.QueryError, match=r"result\[1\]\.values\[1\] is "):
        lp._matrix_rows(body)


def test_matrix_rows_does_not_read_histogram_samples_as_floats():
    lp._matrix_rows(_ok([{"histograms": [[1, {"count": "x"}]]}]))


def test_finite_row_samples_drops_and_counts():
    row = {"values": [[1, "1"], [2, "+Inf"], [3, "NaN"], [4, "-Inf"], [5, "0"]]}
    assert lp.finite_row_samples(row) == ([(1.0, 1.0), (5.0, 0.0)], 3)
    assert [v for _, v in lp.row_samples(row)][1] == math.inf
    assert lp.finite_row_samples({"histograms": []}) == ([], 0)


@pytest.mark.parametrize("body,message", [
    ({"status": "error", "error": "bad_data: parse error"}, "bad_data: parse error"),
    ({"status": "error"}, "Unknown Prometheus error"),
    ({}, "Unknown Prometheus error"),  # an empty body (http_get_json gives {})
])
def test_matrix_rows_carries_prometheus_error_text(body, message):
    with pytest.raises(lp.QueryError, match=f"^{message}$"):
        lp._matrix_rows(body)


def test_prometheus_range_raises_on_transport_error(monkeypatch):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=30: (None, "refused"))
    with pytest.raises(lp.QueryError, match="^refused$"):
        lp.prometheus_range("http://prom", "up", 0, 60, "1m")


def test_prometheus_range_encodes_the_query_once(monkeypatch):
    seen = []
    monkeypatch.setattr(lp, "http_get_json",
                        lambda url, timeout=30: (seen.append((url, timeout)) or _ok([_ROW]), None))
    assert lp.prometheus_range("http://prom", 'up{a="b c"}', 0.4, 60.6, "1m", timeout=7) == [_ROW]
    assert seen == [("http://prom/api/v1/query_range?query=up%7Ba%3D%22b+c%22%7D"
                     "&start=0&end=61&step=1m", 7)]


@pytest.mark.parametrize("answer,defect", [
    ((None, "refused"), "refused"),
    (({}, None), "malformed Alertmanager response: body is not a JSON array"),
    (({"alerts": []}, None), "malformed Alertmanager response: body is not a JSON array"),
    ((["x"], None), "malformed Alertmanager response: alert[0] is not an object"),
    (([{"labels": []}], None), "malformed Alertmanager response: alert[0].labels"),
    (([{"labels": {"tenant": 1}}], None),
     "malformed Alertmanager response: alert[0].labels.tenant is not a string"),
], ids=["error", "empty-body", "object", "item-string", "labels-list", "label-value-int"])
def test_alertmanager_alerts_raises(monkeypatch, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=15: answer)
    with pytest.raises(lp.QueryError) as exc:
        lp.alertmanager_alerts("http://am")
    assert defect in str(exc.value), exc.value
    assert (exc.value.source, exc.value.reason) == ("Alertmanager", "alertmanager_query_error")
    assert exc.value.error_line == f"ERROR: Alertmanager query failed: {exc.value}"


def test_alertmanager_alerts_returns_the_list(monkeypatch):
    seen = []
    alerts = [{"labels": {"alertname": "A"}}, {}]
    monkeypatch.setattr(lp, "http_get_json",
                        lambda url, timeout=15: (seen.append(url) or alerts, None))
    assert lp.alertmanager_alerts("http://am", filters=("tenant=a b",)) == alerts
    assert lp.alertmanager_alerts("http://am") == alerts
    assert seen == ["http://am/api/v2/alerts?filter=tenant%3Da+b", "http://am/api/v2/alerts"]


def test_query_error_is_a_named_caller_error():
    exc = lp.QueryError("timeout")
    assert isinstance(exc, _lib_exitcodes.CallerError)
    assert (exc.source, exc.reason) == ("Prometheus", "prometheus_query_error")
    assert exc.error_line == "ERROR: Prometheus query failed: timeout"
    assert lp.QueryError("x", reason="query_error").reason == "query_error"


# ── uncaught QueryError at the process exit ──────────────────────────

_SCRIPT = """\
import sys
sys.path.insert(0, {tools!r})
import _lib_exitcodes, _lib_prometheus
_lib_prometheus.http_get_json = lambda url, timeout=0: ({answer!r}, None)

def main():
    if {armed!r}:
        _lib_exitcodes.arm_crash_envelope(build=lambda: {{"rows": []}})
    _lib_prometheus.{call}
    print("measured")
    return 0

_lib_exitcodes.run(main)
"""


@pytest.mark.parametrize("armed", [True, False], ids=["json", "text"])
@pytest.mark.parametrize("call,answer,reason,line", [
    ("prometheus_range('http://p', 'up', 0, 60, '1m')", {"status": "success"},
     "prometheus_query_error",
     "ERROR: Prometheus query failed: malformed Prometheus response: "
     "'data' is missing or not an object"),
    ("alertmanager_alerts('http://am')", {"alerts": []}, "alertmanager_query_error",
     "ERROR: Alertmanager query failed: malformed Alertmanager response: "
     "body is not a JSON array"),
], ids=["prometheus", "alertmanager"])
def test_uncaught_query_error_is_rc2_with_its_envelope(tmp_path, armed, call, answer,
                                                       reason, line):
    """A tool that does not catch it still fails closed: rc 2, one stderr
    line, no traceback; in --json mode one envelope naming the source."""
    script = tmp_path / "tool.py"
    script.write_text(_SCRIPT.format(tools=str(TOOLS_DIR), answer=answer, armed=armed,
                                     call=call), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", timeout=60, env=env)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert proc.stderr.strip().splitlines() == [line], proc.stderr
    if armed:
        assert json.loads(proc.stdout) == {"rows": [], "status": "caller_error",
                                           "reason": reason, "error": line}
    else:
        assert proc.stdout == ""


# ── static gate ──────────────────────────────────────────────────────
#
# #3133 batch 1 moved alert_quality, backtest_threshold and
# cardinality_forecasting off their own `PrometheusQueryError` and off the
# tuple range helper. The remaining users are the later batches' work list:
# this allowlist may only get SHORTER (a new user fails; an entry no longer
# true fails too, so a migration has to remove its line).

_TUPLE_HELPERS = frozenset({"http_get_json", "query_prometheus_instant",
                            "query_prometheus_range"})
# Matched on the module's last dotted part, so `scripts.tools._lib_python`,
# `tools._lib_prometheus` and a relative `from ._lib_python import` all count.
_LIB_MODULES = frozenset({"_lib_python", "_lib_prometheus"})
# Raw HTTP in ops tools: urlopen, and the opener API that does the same
# (`build_opener(...).open`, an `OpenerDirector`).
_RAW_HTTP = frozenset({"urlopen", "build_opener", "OpenerDirector"})

# (repo-relative file, name) — the helper a tool imports or calls by attribute.
_ALLOWED = frozenset({
    # the facade re-export goes in batch 4
    ("scripts/tools/_lib_python.py", "http_get_json"),
    ("scripts/tools/_lib_python.py", "query_prometheus_instant"),
    ("scripts/tools/_lib_python.py", "query_prometheus_range"),
    # batch 2: instant callers
    ("scripts/tools/ops/baseline_discovery.py", "http_get_json"),
    ("scripts/tools/ops/baseline_discovery.py", "query_prometheus_instant"),
    ("scripts/tools/ops/byo_check.py", "http_get_json"),
    ("scripts/tools/ops/byo_check.py", "query_prometheus_instant"),
    ("scripts/tools/ops/cardinality_forecasting.py", "query_prometheus_instant"),
    ("scripts/tools/ops/diagnose.py", "http_get_json"),
    ("scripts/tools/ops/diagnose.py", "query_prometheus_instant"),
    ("scripts/tools/ops/federation_check.py", "http_get_json"),
    ("scripts/tools/ops/federation_check.py", "query_prometheus_instant"),
    ("scripts/tools/ops/shadow_verify.py", "http_get_json"),
    ("scripts/tools/ops/shadow_verify.py", "query_prometheus_instant"),
    ("scripts/tools/ops/threshold_recommend.py", "query_prometheus_instant"),
    ("scripts/tools/ops/validate_migration.py", "http_get_json"),
    ("scripts/tools/ops/validate_migration.py", "query_prometheus_instant"),
    # batch 3: direct http_get_json / urlopen callers
    ("scripts/tools/ops/alert_correlate.py", "http_get_json"),
    ("scripts/tools/ops/backtest_threshold.py", "http_get_json"),  # reachability probe
    ("scripts/tools/ops/blind_spot_discovery.py", "http_get_json"),
    ("scripts/tools/ops/check_alert.py", "http_get_json"),
    ("scripts/tools/ops/cutover_tenant.py", "http_get_json"),
    ("scripts/tools/ops/discover_instance_mappings.py", "http_get_json"),
    ("scripts/tools/ops/discover_instance_mappings.py", "urlopen"),
    ("scripts/tools/ops/federation_check.py", "urlopen"),
    ("scripts/tools/ops/maintenance_scheduler.py", "urlopen"),
    ("scripts/tools/ops/notification_tester.py", "urlopen"),
    ("scripts/tools/ops/policy_opa_bridge.py", "urlopen"),  # build_opener
    ("scripts/tools/ops/operator_check.py", "http_get_json"),
    ("scripts/tools/ops/runtime_audit.py", "http_get_json"),
    ("scripts/tools/ops/threshold_govern.py", "http_get_json"),
    ("scripts/tools/ops/threshold_govern.py", "urlopen"),
    ("scripts/tools/ops/_federation_revocation_reconciler.py", "urlopen"),
})


def _tool_files() -> list[Path]:
    out = [p for p in repo_files(".py", root=REPO_ROOT)
           if TOOLS_DIR in p.parents and p.name != "_lib_prometheus.py"]
    assert out, "no tool files enumerated — the gate would pass on nothing"
    return out


def _uses(tree: ast.AST, *, raw_http: bool) -> set[str]:
    """Tuple helpers this module imports from the lib (by name, or ``*``),
    reaches by attribute or by ``getattr(m, "<name>")``; plus raw HTTP
    (reported as ``urlopen``) when *raw_http* (ops tools)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if (node.module or "").rsplit(".", 1)[-1] in _LIB_MODULES:
                found |= names & _TUPLE_HELPERS
                if "*" in names:
                    found.add("*")
            if raw_http and node.module == "urllib.request" and names & _RAW_HTTP:
                found.add("urlopen")
        elif isinstance(node, ast.Attribute):
            if node.attr in _TUPLE_HELPERS:
                found.add(node.attr)
            elif raw_http and node.attr in _RAW_HTTP:
                found.add("urlopen")
        elif isinstance(node, ast.Name):
            if raw_http and node.id in _RAW_HTTP:
                found.add("urlopen")
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id == "getattr" and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant)
              and node.args[1].value in _TUPLE_HELPERS):
            found.add(node.args[1].value)
    return found


def _measured() -> set[tuple[str, str]]:
    got = set()
    for path in _tool_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        raw_http = rel.startswith("scripts/tools/ops/")
        got |= {(rel, name) for name in _uses(tree, raw_http=raw_http)}
    return got


def test_no_tool_defines_its_own_query_error():
    """One QueryError (lib). A tool's own `class PrometheusQueryError` is the
    shape #3133 removes: its callers decide per site whether to fail."""
    own = []
    for path in _tool_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own += [f"{path.relative_to(REPO_ROOT).as_posix()}:{n.lineno} {n.name}"
                for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name.endswith("QueryError")]
    assert not own, own


def test_tuple_helper_users_only_shrink():
    measured = _measured()
    new = sorted(measured - _ALLOWED)
    assert not new, (
        "new use of a (result, err) query helper or raw urlopen — use "
        "_lib_prometheus.prometheus_range / alertmanager_alerts (raise QueryError) "
        f"instead (#3133): {new}")
    stale = sorted(_ALLOWED - measured)
    assert not stale, f"no longer true, remove from _ALLOWED (it only shrinks): {stale}"


@pytest.mark.parametrize("src,raw_http,want", [
    ("from _lib_python import http_get_json\n", False, {"http_get_json"}),
    ("from _lib_prometheus import query_prometheus_range as q\n", False,
     {"query_prometheus_range"}),
    ("import _lib_prometheus as lp\nlp.query_prometheus_instant('u', 'q')\n", False,
     {"query_prometheus_instant"}),
    ("from _lib_python import prometheus_range, QueryError\n", False, set()),
    ("import urllib.request\nurllib.request.urlopen('u')\n", True, {"urlopen"}),
    ("from urllib.request import urlopen\n", True, {"urlopen"}),
    ("import urllib.request\nurllib.request.urlopen('u')\n", False, set()),
    ("import _lib_prometheus as p\nf = getattr(p, 'query_prometheus_range')\n", False,
     {"query_prometheus_range"}),
    ("x = getattr(o, 'prometheus_range')\n", False, set()),
    ("from _lib_prometheus import *\n", False, {"*"}),
    ("from scripts.tools._lib_python import *\n", False, {"*"}),
    ("from ._lib_prometheus import query_prometheus_range\n", False,
     {"query_prometheus_range"}),
    ("from tools._lib_prometheus import http_get_json\n", False, {"http_get_json"}),
    ("from urllib.request import urlopen as uo\n", True, {"urlopen"}),
    ("import urllib.request as ur\nur.urlopen('u')\n", True, {"urlopen"}),
    ("import urllib.request\nurllib.request.build_opener().open('u')\n", True, {"urlopen"}),
    ("from urllib.request import build_opener\no = build_opener()\n", True, {"urlopen"}),
    ("import urllib.request as ur\nd = ur.OpenerDirector()\n", True, {"urlopen"}),
    ("import urllib.request\nurllib.request.build_opener().open('u')\n", False, set()),
], ids=["import", "import-as", "attribute", "new-api", "urlopen-attr", "urlopen-import",
        "urlopen-outside-ops", "getattr", "getattr-other-name", "star", "star-dotted",
        "relative", "package-prefix", "urlopen-alias", "urllib-alias", "build-opener-attr",
        "build-opener-import", "opener-director", "opener-outside-ops"])
def test_gate_predicate(src, raw_http, want):
    """The predicate itself: each spelling is seen; the raising API is not."""
    assert _uses(ast.parse(src), raw_http=raw_http) == want
