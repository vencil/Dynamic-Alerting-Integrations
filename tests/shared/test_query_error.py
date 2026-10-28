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
  set of tools still on the tuple helpers only shrinks (raw HTTP is
  observed at run time: ``test_ops_http_through_lib``).
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


@pytest.mark.parametrize("raw", ["abc", "", True, None, [1]])
def test_sample_value_refuses_what_is_not_a_sample(raw):
    """#3213: garbage (and a boolean) is not an answer, refused at the one
    reading."""
    with pytest.raises(ValueError):
        lp.sample_value(raw)


@pytest.mark.parametrize("raw", [1, 1.5, 0])
def test_sample_value_refuses_a_json_number(raw):
    """#3304: every server writes a sample value as a JSON string; a JSON
    number is not one, and is refused like any other unreadable sample."""
    with pytest.raises(ValueError, match="not a sample value"):
        lp.sample_value(raw)


@pytest.mark.parametrize("raw,want", [(1, 1.0), (1700000000.123, 1700000000.123)])
def test_sample_timestamp_reads_a_json_number(raw, want):
    assert lp.sample_timestamp(raw) == want


@pytest.mark.parametrize("raw", ["1700000000", True, None, [1]])
def test_sample_timestamp_refuses_anything_else(raw):
    with pytest.raises(ValueError, match="not a sample timestamp"):
        lp.sample_timestamp(raw)


@pytest.mark.parametrize("key, sample", [
    ("values", ["abc", "1"]), ("values", [None, "2"]), ("values", ["1", "2"]), ("values", [1, 2]),
    ("histograms", ["1", {"count": "1"}]),
])
def test_matrix_rows_refuses_a_sample_not_of_the_wire_shape(key, sample):
    """#3304: a timestamp that is not a number, or a float sample that is not
    a string, is a malformed answer (it used to pass: the timestamp was not
    read and a numeric value was accepted)."""
    with pytest.raises(lp.QueryError, match=rf"result\[0\]\.{key}\[0\] is "):
        lp._matrix_rows(_ok([{"metric": {}, key: [sample]}]))


@pytest.mark.parametrize("key, sample", [
    ("value", ["1", "1"]), ("value", [1, 1]), ("histogram", [None, {"count": "1"}]),
])
def test_vector_rows_refuses_a_sample_not_of_the_wire_shape(key, sample):
    with pytest.raises(lp.QueryError, match=rf"result\[0\]\.{key} is "):
        lp._vector_rows({"status": "success", "data": {"resultType": "vector",
                                                       "result": [{"metric": {}, key: sample}]}})


@pytest.mark.parametrize("value", ["abc", True, None])
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


# ── instant reader (#3133 batch 2) ───────────────────────────────────

def _vec(result, result_type="vector"):
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


_VROW = {"metric": {"tenant": "t"}, "value": [1, "3"]}


@pytest.mark.parametrize("body", [
    _vec([]), _vec([_VROW]), _vec([{"value": [1, "NaN"]}]),
    _vec([{"metric": {}, "histogram": [1, {"count": "x"}]}]),
], ids=["empty", "one-row", "nan-without-metric", "histogram-only"])
def test_vector_rows_accepts_a_vector(body):
    assert lp._vector_rows(body) == body["data"]["result"]


@pytest.mark.parametrize("body,defect", [
    (None, "body is not a JSON object"),
    ({"status": "success"}, "'data' is missing or not an object"),
    (_vec([], "matrix"), "resultType is 'matrix', expected 'vector'"),
    (_vec(None), "'data.result' is missing or not a list"),
    (_vec(["x"]), "result[0] is not an object"),
    (_vec([{"metric": [], "value": [1, "1"]}]), "result[0].metric is not an object"),
    (_vec([{"metric": {}}]), "result[0] has neither 'value' nor 'histogram'"),
    (_vec([{"value": [1]}]), "result[0].value is [1]"),
    (_vec([{"value": [1, "abc"]}]), "result[0].value is [1, 'abc']"),
    (_vec([{"value": [1, True]}]), "result[0].value is [1, True]"),
], ids=["null", "no-data", "matrix", "result-null", "row-string", "metric-list",
        "no-value", "value-short", "value-not-number", "value-bool"])
def test_vector_rows_names_the_defect(body, defect):
    with pytest.raises(lp.QueryError) as exc:
        lp._vector_rows(body)
    assert str(exc.value).startswith("malformed Prometheus response: "), exc.value
    assert defect in str(exc.value), exc.value


def test_vector_rows_carries_prometheus_error_text():
    with pytest.raises(lp.QueryError, match="^bad_data: x$"):
        lp._vector_rows({"status": "error", "error": "bad_data: x"})


def test_prometheus_instant(monkeypatch):
    seen = []
    monkeypatch.setattr(lp, "http_get_json",
                        lambda url, timeout=10: (seen.append((url, timeout)) or (_vec([_VROW]), None)))
    assert lp.prometheus_instant("http://p", 'up{job="a b"}', timeout=3) == [_VROW]
    assert seen == [("http://p/api/v1/query?query=up%7Bjob%3D%22a+b%22%7D", 3)]
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: (None, "refused"))
    with pytest.raises(lp.QueryError, match="^refused$"):
        lp.prometheus_instant("http://p", "up")


def test_vector_sample():
    assert lp.vector_sample(_VROW) == 3.0
    assert lp.vector_sample({"histogram": [1, {}]}) is None
    assert math.isinf(lp.vector_sample({"value": [1, "+Inf"]}))


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


@pytest.mark.parametrize("rows,want", [
    ([], None),
    ([{"value": [1, "3"]}, {"value": [1, "9"]}], 3),
    ([{"value": [1, "2.0"]}], 2),
], ids=["empty", "first-row", "float-string"])
def test_vector_count(rows, want):
    assert lp.vector_count(rows) == want


@pytest.mark.parametrize("row", [
    {"value": [1, "NaN"]}, {"value": [1, "+Inf"]}, {"value": [1, "-Inf"]},
    {"histogram": [1, {}]},
], ids=["nan", "inf", "-inf", "histogram"])
def test_vector_count_refuses_what_is_not_a_count(row):
    """#3133: int(float()) on these used to crash the check tools."""
    with pytest.raises(lp.QueryError, match="expected a finite count") as exc:
        lp.vector_count([row])
    assert exc.value.reason == "prometheus_query_error"


def test_instant_or_error(monkeypatch):
    """The answer is checked as prometheus_instant checks it; only the error
    is a value."""
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: (_vec([_VROW]), None))
    assert lp.instant_or_error("http://p", "up") == ([_VROW], None)
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: (None, "refused"))
    assert lp.instant_or_error("http://p", "up") == (None, "refused")
    monkeypatch.setattr(lp, "http_get_json",
                        lambda url, timeout=10: (_vec([{"metric": {}}]), None))
    rows, err = lp.instant_or_error("http://p", "up")
    assert rows is None
    assert err == "malformed Prometheus response: result[0] has neither 'value' nor 'histogram'"


@pytest.mark.parametrize("answer,defect", [
    ((None, "refused"), "refused"),
    (({}, None), "malformed Alertmanager response: body is not a JSON array"),
    ((["x"], None), "malformed Alertmanager response: silence[0] is not an object"),
    (([{"status": "active"}], None),
     "malformed Alertmanager response: silence[0].status is not an object"),
    (([{"status": {"state": 1}}], None),
     "malformed Alertmanager response: silence[0].status.state is not a string"),
], ids=["error", "object", "item-string", "status-string", "state-int"])
def test_alertmanager_silences_raises(monkeypatch, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=15: answer)
    with pytest.raises(lp.QueryError) as exc:
        lp.alertmanager_silences("http://am")
    assert str(exc.value) == defect
    assert (exc.value.source, exc.value.reason) == ("Alertmanager", "alertmanager_query_error")


def test_alertmanager_silences_returns_the_list(monkeypatch):
    seen = []
    silences = [{"status": {"state": "active"}}, {"id": "2"}]
    monkeypatch.setattr(lp, "http_get_json",
                        lambda url, timeout=15: (seen.append((url, timeout)) or silences, None))
    assert lp.alertmanager_silences("http://am", timeout=4) == silences
    assert seen == [("http://am/api/v2/silences", 4)]


@pytest.mark.parametrize("call,answer,defect", [
    ("alerts", (None, "refused"), "refused"),
    ("alerts", ({"status": "error", "error": "bad_data"}, None), "bad_data"),
    ("alerts", ([], None), "malformed Prometheus response: body is not a JSON object"),
    ("alerts", ({"status": "success"}, None), "'data' is missing or not an object"),
    ("alerts", ({"status": "success", "data": {}}, None), "'data.alerts' is missing or not a list"),
    ("alerts", ({"status": "success", "data": {"alerts": ["x"]}}, None), "alert[0] is not an object"),
    ("alerts", ({"status": "success", "data": {"alerts": [{"labels": []}]}}, None),
     "alert[0].labels is not an object"),
    ("alerts", ({"status": "success", "data": {"alerts": [{"labels": {"a": 1}}]}}, None),
     "alert[0].labels.a is not a string"),
    ("targets", ({"status": "success", "data": {}}, None),
     "'data.activeTargets' is missing or not a list"),
    ("targets", ({"status": "success", "data": {"activeTargets": [{"labels": {"job": 1}}]}}, None),
     "target[0].labels.job is not a string"),
], ids=["a-refused", "a-error", "a-list", "a-no-data", "a-no-key", "a-item", "a-labels",
        "a-label-int", "t-no-key", "t-label-int"])
def test_prometheus_alerts_and_targets_raise(monkeypatch, call, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: answer)
    read = lp.prometheus_alerts if call == "alerts" else lp.prometheus_targets
    with pytest.raises(lp.QueryError) as exc:
        read("http://p")
    assert defect in str(exc.value), exc.value
    assert exc.value.reason == "prometheus_query_error"


def test_prometheus_alerts_and_targets_return_the_rows(monkeypatch):
    seen = []
    alerts = [{"labels": {"alertname": "A"}}, {}]
    targets = [{"labels": {"job": "j"}, "health": "up"}]

    def get(url, timeout=10):
        seen.append((url, timeout))
        key, rows = ("alerts", alerts) if "/alerts" in url else ("activeTargets", targets)
        return {"status": "success", "data": {key: rows}}, None
    monkeypatch.setattr(lp, "http_get_json", get)
    assert lp.prometheus_alerts("http://p", timeout=3) == alerts
    assert lp.prometheus_targets("http://p", state="any") == targets
    assert seen == [("http://p/api/v1/alerts", 3), ("http://p/api/v1/targets?state=any", 10)]


@pytest.mark.parametrize("answer,defect", [
    ((None, "refused"), "refused"),
    (([], None), "malformed Alertmanager response: body is not a JSON object"),
    (({"config": "x"}, None), "'config' is missing or not an object"),
    (({"config": {"original": 5}}, None), "'config.original' is missing or not a string"),
    (({}, None), "'config' is missing or not an object"),
    (({"config": {}}, None), "'config.original' is missing or not a string"),
], ids=["refused", "list", "config-string", "original-int", "no-config", "no-original"])
def test_alertmanager_status_raises(monkeypatch, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: answer)
    with pytest.raises(lp.QueryError) as exc:
        lp.alertmanager_status("http://am")
    assert defect in str(exc.value)
    assert (exc.value.source, exc.value.reason) == ("Alertmanager", "alertmanager_query_error")


def test_alertmanager_status_returns_the_object(monkeypatch):
    body = {"config": {"original": "route: {}"}, "cluster": {}}
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: (body, None))
    assert lp.alertmanager_status("http://am") == body


@pytest.mark.parametrize("call,answer,defect", [
    ("series", (None, "refused"), "refused"),
    ("series", ({"status": "error", "error": "bad_data"}, None), "bad_data"),
    ("series", ([], None), "body is not a JSON object"),
    ("series", ({"status": "success"}, None), "'data' is missing or not a list"),
    ("series", ({"status": "success", "data": ["x"]}, None), "series[0] is not an object"),
    ("series", ({"status": "success", "data": [{"job": 1}]}, None), "series[0].job is not a string"),
    ("values", ({"status": "success", "data": {"x": 1}}, None), "'data' is missing or not a list"),
    ("values", ({"status": "success", "data": ["a", 2]}, None), "data[1] is not a string"),
], ids=["s-refused", "s-error", "s-list", "s-no-data", "s-item", "s-label-int",
        "v-object", "v-int"])
def test_prometheus_series_and_label_values_raise(monkeypatch, call, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: answer)
    with pytest.raises(lp.QueryError) as exc:
        if call == "series":
            lp.prometheus_series("http://p", '{job="j"}')
        else:
            lp.prometheus_label_values("http://p", "job")
    assert defect in str(exc.value), exc.value
    assert exc.value.reason == "prometheus_query_error"


def test_prometheus_series_and_label_values_return_the_data(monkeypatch):
    seen = []

    def get(url, timeout=10):
        seen.append((url, timeout))
        data = [{"job": "j"}] if "/series" in url else ["a", "b"]
        return {"status": "success", "data": data}, None
    monkeypatch.setattr(lp, "http_get_json", get)
    assert lp.prometheus_series("http://p", '{a="x y"}', timeout=3) == [{"job": "j"}]
    assert lp.prometheus_label_values("http://p", "job") == ["a", "b"]
    assert seen == [("http://p/api/v1/series?match%5B%5D=%7Ba%3D%22x+y%22%7D", 3),
                    ("http://p/api/v1/label/job/values", 10)]


def test_rules_groups_reads_rules_null_as_an_empty_group():
    """#3331: a Go server that builds the slice as nil (Thanos has no
    omitempty either) writes an empty group as `rules: null` — an answer,
    returned with `rules` a list and the body left as it was."""
    body = {"status": "success", "data": {"groups": [{"name": "g", "rules": None}]}}
    assert lp.rules_groups(body) == [{"name": "g", "rules": []}]
    assert body["data"]["groups"][0]["rules"] is None


@pytest.mark.parametrize("body,defect", [
    ([], "body is not a JSON object"),
    ({"status": "error", "error": "bad_data"}, "bad_data"),
    ({"status": "success", "data": None}, "'data' is missing or not an object"),
    ({"status": "success", "data": {}}, "'data.groups' is missing or not a list"),
    ({"status": "success", "data": {"groups": ["x"]}}, "group[0] is not an object"),
    ({"status": "success", "data": {"groups": [{"name": "g", "rules": "x"}]}}, "group[0].rules is not a list"),
    ({"status": "success", "data": {"groups": [{"name": "g", "rules": [{}, 1]}]}},
     "group[0].rules[1] is not an object"),
    # #3331: Prometheus always writes `rules`; a group without it is malformed
    ({"status": "success", "data": {"groups": [{"name": "g"}]}}, "group[0] has no 'rules'"),
    # #3331: the fields a reader calls string / list methods on
    ({"status": "success", "data": {"groups": [{"rules": []}]}}, "group[0].name is missing or not a string"),
    ({"status": "success", "data": {"groups": [{"name": None, "rules": []}]}},
     "group[0].name is missing or not a string"),
    ({"status": "success", "data": {"groups": [{"name": "g", "rules": [{"lastError": 1}]}]}},
     "group[0].rules[0].lastError is not a string"),
    ({"status": "success", "data": {"groups": [{"name": "g", "rules": [{"alerts": None}]}]}},
     "group[0].rules[0].alerts is not a list"),
    ({"status": "success", "data": {"groups": [{"name": "g", "rules": [{"name": "A"}, {"name": 5}]}]}},
     "group[0].rules[1].name is not a string"),
], ids=["list", "error", "data-null", "no-groups", "group-string", "rules-string", "rule-int",
        "no-rules", "no-name", "name-null", "lastError-int", "alerts-null", "rule-name-int"])
def test_rules_groups_raises(body, defect):
    with pytest.raises(lp.QueryError) as exc:
        lp.rules_groups(body)
    assert defect in str(exc.value), exc.value


def test_prometheus_rules_returns_the_groups(monkeypatch):
    groups = [{"name": "g", "rules": [{"name": "A"}]}, {"name": "empty", "rules": []}]
    seen = []
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=15: (
        seen.append((url, timeout)) or {"status": "success", "data": {"groups": groups}}, None))
    assert lp.prometheus_rules("http://p") == groups
    assert seen == [("http://p/api/v1/rules", 15)]
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=15: (None, "refused"))
    with pytest.raises(lp.QueryError, match="refused"):
        lp.prometheus_rules("http://p")


@pytest.mark.parametrize("answer,defect", [
    ((None, "refused"), "refused"),
    (({"status": "error", "error": "bad_data"}, None), "bad_data"),
    (([], None), "body is not a JSON object"),
    (({"status": "success", "data": None}, None), "'data' is missing or not an object"),
    (({"status": "success", "data": {}}, None), "'data.yaml' is missing or not a string"),
    (({"status": "success", "data": {"yaml": 5}}, None), "'data.yaml' is missing or not a string"),
], ids=["refused", "error", "list", "data-null", "no-yaml", "yaml-int"])
def test_prometheus_status_config_raises(monkeypatch, answer, defect):
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: answer)
    with pytest.raises(lp.QueryError) as exc:
        lp.prometheus_status_config("http://p")
    assert defect in str(exc.value), exc.value
    assert exc.value.reason == "prometheus_query_error"


def test_prometheus_status_config_returns_the_yaml(monkeypatch):
    seen = []
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=10: (
        seen.append(url) or {"status": "success", "data": {"yaml": "global: {}\n"}}, None))
    assert lp.prometheus_status_config("http://p") == "global: {}\n"
    assert seen == ["http://p/api/v1/status/config"]


@pytest.mark.parametrize("rule_type,path", [
    (None, "/api/v1/rules"), ("alert", "/api/v1/rules?type=alert"), ("record", "/api/v1/rules?type=record")])
def test_prometheus_rules_asks_for_the_type(monkeypatch, rule_type, path):
    seen = []
    monkeypatch.setattr(lp, "http_get_json", lambda url, timeout=15: (
        seen.append(url) or {"status": "success", "data": {"groups": []}}, None))
    assert lp.prometheus_rules("http://p", rule_type=rule_type) == []
    assert seen == ["http://p" + path]


def test_prometheus_rules_refuses_an_unknown_type():
    with pytest.raises(ValueError):
        lp.prometheus_rules("http://p", rule_type="alerts")


def test_the_facade_does_not_re_export_the_tuple_readers():
    """#3133 batch 4: a tool takes a tuple reader from _lib_prometheus, by
    name — the shrink-only gate above sees that import."""
    import _lib_python
    assert not [n for n in ("http_get_json", "query_prometheus_instant", "query_prometheus_range")
                if hasattr(_lib_python, n)]


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

# `instant_or_error` checks the answer as prometheus_instant does but still
# hands back `(rows, err)`, for the three check tools whose judges take a
# result and its error (#3133 batch 2b). An `err` can still be dropped, so it
# is held to the same list: no tool beyond those three may start using it.
_TUPLE_HELPERS = frozenset({"http_get_json", "query_prometheus_instant",
                            "query_prometheus_range", "instant_or_error"})
# Matched on the module's last dotted part, so `scripts.tools._lib_python`,
# `tools._lib_prometheus` and a relative `from ._lib_python import` all count.
_LIB_MODULES = frozenset({"_lib_python", "_lib_prometheus"})
# Raw HTTP (urlopen, an opener, a socket…) is not matched by name here any
# more: every spelling a review found got past a name list. Whether an ops
# tool opens connections without the lib is observed at run time —
# tests/shared/test_ops_http_through_lib.py (#3215).

# (repo-relative file, name) — the helper a tool imports or calls by attribute.
_ALLOWED = frozenset({
    # batch 2: instant callers
    ("scripts/tools/ops/byo_check.py", "instant_or_error"),
    ("scripts/tools/ops/federation_check.py", "instant_or_error"),
    ("scripts/tools/ops/shadow_verify.py", "instant_or_error"),
    # not Prometheus / Alertmanager reads
    ("scripts/tools/ops/backtest_threshold.py", "http_get_json"),  # reachability probe
    ("scripts/tools/ops/threshold_govern.py", "http_get_json"),  # tenant-api
})


def _tool_files() -> list[Path]:
    out = [p for p in repo_files(".py", root=REPO_ROOT)
           if TOOLS_DIR in p.parents and p.name != "_lib_prometheus.py"]
    assert out, "no tool files enumerated — the gate would pass on nothing"
    return out


def _uses(tree: ast.AST) -> set[str]:
    """Tuple helpers this module imports from the lib (by name, or ``*``),
    reaches by attribute or by ``getattr(m, "<name>")``. A floor, not a proof:
    a name built at run time gets past it (#3215)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            if (node.module or "").rsplit(".", 1)[-1] in _LIB_MODULES:
                found |= names & _TUPLE_HELPERS
                if "*" in names:
                    found.add("*")
        elif isinstance(node, ast.Attribute):
            if node.attr in _TUPLE_HELPERS:
                found.add(node.attr)
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
        got |= {(rel, name) for name in _uses(tree)}
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
        "new use of a (result, err) query helper — use "
        "_lib_prometheus.prometheus_range / alertmanager_alerts (raise QueryError) "
        f"instead (#3133): {new}")
    stale = sorted(_ALLOWED - measured)
    assert not stale, f"no longer true, remove from _ALLOWED (it only shrinks): {stale}"


@pytest.mark.parametrize("src,want", [
    ("from _lib_python import http_get_json\n", {"http_get_json"}),
    ("from _lib_prometheus import query_prometheus_range as q\n", {"query_prometheus_range"}),
    ("import _lib_prometheus as lp\nlp.query_prometheus_instant('u', 'q')\n",
     {"query_prometheus_instant"}),
    ("from _lib_python import prometheus_range, QueryError\n", set()),
    ("import _lib_prometheus as p\nf = getattr(p, 'query_prometheus_range')\n",
     {"query_prometheus_range"}),
    ("x = getattr(o, 'prometheus_range')\n", set()),
    ("from _lib_prometheus import *\n", {"*"}),
    ("from scripts.tools._lib_python import *\n", {"*"}),
    ("from ._lib_prometheus import query_prometheus_range\n", {"query_prometheus_range"}),
    ("from tools._lib_prometheus import http_get_json\n", {"http_get_json"}),
    ("import urllib.request\nurllib.request.urlopen('u')\n", set()),
], ids=["import", "import-as", "attribute", "new-api", "getattr", "getattr-other-name", "star",
        "star-dotted", "relative", "package-prefix", "raw-http-is-the-runtime-gates"])
def test_gate_predicate(src, want):
    """The predicate itself: each spelling is seen; the raising API is not."""
    assert _uses(ast.parse(src)) == want


def test_row_samples_reads_the_timestamp_through_the_lib():
    """#3304: `row_samples` reads each timestamp with `sample_timestamp`, not
    `float()` — a row that skipped `_matrix_rows` still refuses a string one."""
    assert lp.row_samples({"values": [[1, "2"]]}) == [(1.0, 2.0)]
    with pytest.raises(ValueError, match="not a sample timestamp"):
        lp.row_samples({"values": [["1", "2"]]})
