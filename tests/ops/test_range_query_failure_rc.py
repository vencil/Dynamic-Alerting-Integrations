#!/usr/bin/env python3
"""Range 查詢沒量成時的結束碼（#3133 第一批：#3073、#3074）。

三支用 ``/api/v1/query_range`` 的工具（alert_quality、backtest_threshold、
cardinality_forecasting）與 alert_quality 的 Alertmanager 查詢，在下列情況
都必須以 rc 2 結束：連不上、HTTP 錯誤、``status`` 非 success、以及
**回 200 + success 但形狀不對**（缺 ``data``、``result`` 不是 list、列沒有
``values``⋯）。以前後者被讀成「沒有資料」（rc 0）或在工具裡崩潰。

每種形狀都在一列正常的資料**之後**出現，所以「rc 不取決於這輪處理了哪些
項目」也一起被量：前面那列量成了，報告也不能因此當成 ok。

全部以子行程跑真的工具，對本機假服務（不 patch 工具內部）：
``scripts/tools/ops/alert_quality.py``、``scripts/tools/ops/backtest_threshold.py``、
``scripts/tools/ops/cardinality_forecasting.py``；形狀檢查本身在
``scripts/tools/_lib_prometheus.py``。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_OPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                    "scripts", "tools", "ops")

_NOW = time.time()

# 每支工具的「正常一列」：能被量成、不會讓工具自己判 rc 非 0 的資料。
_GOOD_ROW = {
    "alert_quality": {
        "metric": {"alertname": "HostDown", "tenant": "tenant-one", "alertstate": "firing"},
        "values": [[_NOW - 120, "1"], [_NOW - 60, "1"]],
    },
    # backtest: 10 points at 40 — below both 70 and 50, so the change is LOW (rc 0)
    "backtest_threshold": {"metric": {}, "values": [[i, "40"] for i in range(10)]},
    # cardinality: flat 10 series over 30 days, limit 500 → safe (rc 0)
    "cardinality_forecasting": {
        "metric": {"tenant": "tenant-one"},
        "values": [[_NOW - d * 86400, "10"] for d in range(30, 0, -1)],
    },
}

# Malformed success bodies. A callable gets the tool's good row so a
# row-level defect can sit next to a measured row.
_SHAPES = {
    "no_data": lambda good: {"status": "success"},
    "data_is_string": lambda good: {"status": "success", "data": "x"},
    "result_null": lambda good: {"status": "success",
                                 "data": {"resultType": "matrix", "result": None}},
    "result_object": lambda good: {"status": "success",
                                   "data": {"resultType": "matrix", "result": {"a": 1}}},
    "row_is_string": lambda good: {"status": "success",
                                   "data": {"resultType": "matrix", "result": [good, "x"]}},
    "row_without_values": lambda good: {
        "status": "success",
        "data": {"resultType": "matrix", "result": [good, {"metric": {"tenant": "t2"}}]}},
    "values_not_list": lambda good: {
        "status": "success",
        "data": {"resultType": "matrix",
                 "result": [good, {"metric": {"tenant": "t2"}, "values": "x"}]}},
    "sample_not_pair": lambda good: {
        "status": "success",
        "data": {"resultType": "matrix",
                 "result": [good, {"metric": {"tenant": "t2"}, "values": [[1]]}]}},
    # #3213: a sample value the lib cannot read is the answer's defect, named
    "sample_not_number": lambda good: {
        "status": "success",
        "data": {"resultType": "matrix",
                 "result": [good, {"metric": {"tenant": "t2"}, "values": [[1, "abc"]]}]}},
    "sample_is_bool": lambda good: {
        "status": "success",
        "data": {"resultType": "matrix",
                 "result": [good, {"metric": {"tenant": "t2"}, "values": [[1, True]]}]}},
    "result_type_vector": lambda good: {
        "status": "success",
        "data": {"resultType": "vector", "result": [{"metric": {}, "value": [1, "1"]}]}},
}


@contextlib.contextmanager
def _fake(range_body, am=None):
    """One local server for Prometheus and Alertmanager.

    *range_body*: the JSON ``/api/v1/query_range`` answers with (200).
    *am*: ``None`` (no AM route: 404), ``("ok", n)`` n suppressed alerts,
    ``("500",)``, or ``("body", obj)`` 200 with *obj*.
    """

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            path = urllib.parse.urlparse(self.path).path
            code, body = 404, {"status": "error", "error": "not found"}
            if path == "/api/v1/status/buildinfo":
                code, body = 200, {"status": "success", "data": {"version": "2.51.0"}}
            elif path == "/api/v1/query":
                code, body = 200, {"status": "success",
                                   "data": {"resultType": "vector", "result": []}}
            elif path == "/api/v1/query_range":
                code, body = 200, range_body
            elif path == "/api/v2/alerts" and am is not None:
                if am[0] == "ok":
                    code, body = 200, [{"labels": {"alertname": "HostDown",
                                                   "tenant": "tenant-one"}}] * am[1]
                elif am[0] == "500":
                    code, body = 500, {"error": "boom"}
                else:
                    code, body = 200, am[1]
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *a):  # noqa: D401
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _run(tool, *argv, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["NO_PROXY"] = env["no_proxy"] = "*"
    env["DA_LANG"] = "en"
    return subprocess.run(
        [sys.executable, os.path.join(_OPS, f"{tool}.py"), *argv],
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env, cwd=cwd)


def _good(tool):
    return {"status": "success",
            "data": {"resultType": "matrix", "result": [_GOOD_ROW[tool]]}}


def _argv(tool, url, out=None):
    if tool == "alert_quality":
        return ["--prometheus", url, "--json", "--ci"]
    if tool == "backtest_threshold":
        return ["--tenant", "t", "--metric", "mysql_connections", "--old-value", "70",
                "--new-value", "50", "--prometheus", url, "--json", "-o", str(out)]
    return ["--prometheus", url, "--json", "--ci"]


_TOOLS = ["alert_quality", "backtest_threshold", "cardinality_forecasting"]


# ── Prometheus range shapes ──────────────────────────────────────────

# A native-histogram row has `histograms` and no `values` (the API omits the
# one a series has no samples of). The tools read float samples only and
# have always skipped it: it is an answered row, so the rc stays the good
# row's (0), and the row is still not measured as float data.
_HISTOGRAM_ROW = {"metric": {"tenant": "t2", "alertname": "X"},
                  "histograms": [[1, {"count": "3", "sum": "1", "buckets": []}]]}


@pytest.mark.parametrize("tool", _TOOLS)
def test_histogram_only_row_is_skipped_as_before(tool, tmp_path):
    body = {"status": "success", "data": {"resultType": "matrix",
                                          "result": [_GOOD_ROW[tool], _HISTOGRAM_ROW]}}
    with _fake(body) as url:
        proc = _run(tool, *_argv(tool, url, tmp_path / "o.json"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert doc.get("status") not in ("caller_error", "partial"), doc
    assert "malformed" not in proc.stderr and "Traceback" not in proc.stderr, proc.stderr
    if tool == "backtest_threshold":
        assert doc["changes"][0]["status"] == "analyzed", doc
        assert doc["changes"][0]["data_points"] == len(_GOOD_ROW[tool]["values"]), doc
    elif tool == "alert_quality":
        # the histogram row's series appears with no samples, as on main
        names = {a["alertname"] for t in doc["tenants"] for a in t["alerts"]}
        assert "HostDown" in names, doc
    else:
        assert [t["tenant"] for t in doc["tenants"]] == ["tenant-one"], doc

@pytest.mark.parametrize("tool", _TOOLS)
def test_good_matrix_is_rc0(tool, tmp_path):
    """Control: the good row alone is measured and the run is rc 0 — so a
    shape case below that turns rc 2 is the shape, not the row."""
    with _fake(_good(tool)) as url:
        proc = _run(tool, *_argv(tool, url, tmp_path / "o.json"))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "status" not in json.loads(proc.stdout) or \
        json.loads(proc.stdout)["status"] not in ("caller_error", "partial")


@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("tool", ["alert_quality", "cardinality_forecasting"])
def test_malformed_success_is_a_named_caller_error(tool, shape):
    """alert_quality / cardinality_forecasting: one caller_error envelope,
    reason ``prometheus_query_error``, rc 2, the defect named."""
    with _fake(_SHAPES[shape](_GOOD_ROW[tool])) as url:
        proc = _run(tool, *_argv(tool, url))
    assert proc.returncode == 2, (shape, proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert (doc["status"], doc["reason"]) == ("caller_error", "prometheus_query_error"), doc
    assert "malformed Prometheus response" in doc["error"], doc
    assert "Traceback" not in proc.stderr, proc.stderr
    assert "malformed Prometheus response" in proc.stderr, proc.stderr


@pytest.mark.parametrize("shape", sorted(_SHAPES))
def test_backtest_malformed_success_is_query_error(shape, tmp_path):
    """backtest_threshold (#3059 rule): the change is ``query_error``, the
    report ``status: partial`` / ``reason: query_error``, rc 2, and ``-o``
    is written with the same document."""
    out = tmp_path / "o.json"
    with _fake(_SHAPES[shape](_GOOD_ROW["backtest_threshold"])) as url:
        proc = _run("backtest_threshold", *_argv("backtest_threshold", url, out))
    assert proc.returncode == 2, (shape, proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert (doc["status"], doc["reason"]) == ("partial", "query_error"), doc
    row = doc["changes"][0]
    assert row["status"] == "query_error", row
    assert "malformed Prometheus response" in row["message"], row
    assert json.loads(out.read_text(encoding="utf-8")) == doc
    assert "Traceback" not in proc.stderr, proc.stderr


# ── alert_quality: no Alertmanager (#3212) ───────────────────────────
# The suppression dimension, and with it `--alertmanager` and the #3073
# Alertmanager-not-answering cases, are gone: it was never measured right.

@pytest.mark.parametrize("extra", [["--json", "--ci"], ["--markdown"]], ids=["json-ci", "markdown"])
def test_alertmanager_flag_is_a_usage_error(extra):
    with _fake(_good("alert_quality")) as url:
        proc = _run("alert_quality", "--prometheus", url, "--alertmanager", url, *extra)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "--alertmanager" in proc.stderr and proc.stdout == "", (proc.stdout, proc.stderr)


def test_the_report_has_no_suppression_fields():
    with _fake(_good("alert_quality")) as url:
        proc = _run("alert_quality", "--prometheus", url, "--json")
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    alert = json.loads(proc.stdout)["tenants"][0]["alerts"][0]
    assert not [k for k in alert if k.startswith("suppress")], alert


# ── non-finite samples (#3213) ───────────────────────────────────────

_NON_FINITE = ["+Inf", "-Inf", "NaN"]


@pytest.mark.parametrize("value", _NON_FINITE)
def test_alert_quality_drops_and_discloses_non_finite(value):
    """ALERTS is 0 / 1: a non-finite sample is no state. Left out, counted,
    rc by the analysis (0) — not an internal error (rc 2) nor a silent skip."""
    row = {"metric": {"alertname": "HostDown", "tenant": "tenant-one"},
           "values": [[_NOW - 180, "1"], [_NOW - 120, value], [_NOW - 60, "1"]]}
    body = {"status": "success", "data": {"resultType": "matrix", "result": [row]}}
    with _fake(body) as url:
        proc = _run("alert_quality", *_argv("alert_quality", url))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert doc["summary"]["non_finite_samples_dropped"] == 1, doc["summary"]
    (alert,) = doc["tenants"][0]["alerts"]
    assert (alert["non_finite_samples_dropped"], alert["total_alerts"]) == (1, 2), alert
    with _fake(body) as url:
        text = _run("alert_quality", "--prometheus", url)
    assert "1 non-finite sample value(s)" in text.stdout, text.stdout


@pytest.mark.parametrize("value", _NON_FINITE)
def test_cardinality_drops_and_discloses_non_finite(value):
    good = _GOOD_ROW["cardinality_forecasting"]
    row = dict(good, values=good["values"][:-1] + [[good["values"][-1][0], value]])
    body = {"status": "success", "data": {"resultType": "matrix", "result": [row]}}
    with _fake(body) as url:
        proc = _run("cardinality_forecasting", *_argv("cardinality_forecasting", url))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert doc["summary"]["non_finite_samples_dropped"] == 1, doc["summary"]
    (t,) = doc["tenants"]
    assert (t["data_points"], t["current_cardinality"]) == (len(good["values"]) - 1, 10), t
    with _fake(body) as url:
        text = _run("cardinality_forecasting", "--prometheus", url)
    assert "1 non-finite sample value(s)" in text.stdout, text.stdout


def test_cardinality_all_non_finite_is_no_data_and_says_how_many():
    row = {"metric": {"tenant": "tenant-one"},
           "values": [[_NOW - d * 86400, "NaN"] for d in range(5, 0, -1)]}
    body = {"status": "success", "data": {"resultType": "matrix", "result": [row]}}
    with _fake(body) as url:
        proc = _run("cardinality_forecasting", *_argv("cardinality_forecasting", url))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert (doc["status"], doc["reason"]) == ("no_data", "no_cardinality_data"), doc
    assert doc["summary"]["non_finite_samples_dropped"] == 5, doc["summary"]
    assert "Traceback" not in proc.stderr


def test_backtest_keeps_inf_as_a_sample(tmp_path):
    """backtest compares +Inf like Alertmanager would: it is a breach of an
    upper threshold, not a gap (#2963); NaN alone stays no_data."""
    row = {"metric": {}, "values": [[i, "40"] for i in range(9)] + [[9, "+Inf"]]}
    body = {"status": "success", "data": {"resultType": "matrix", "result": [row]}}
    with _fake(body) as url:
        proc = _run("backtest_threshold", *_argv("backtest_threshold", url, tmp_path / "o.json"))
    assert proc.returncode in (0, 1), (proc.returncode, proc.stdout, proc.stderr)
    change = json.loads(proc.stdout)["changes"][0]
    assert (change["status"], change["data_points"]) == ("analyzed", 10), change
    assert (change["old_breach_count"], change["new_breach_count"]) == (1, 1), change


def _two_tenants(a_values, b_values):
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"tenant": "a"}, "values": a_values},
        {"metric": {"tenant": "b"}, "values": b_values}]}}


def test_cardinality_disclosure_counts_only_the_tenant_asked_for():
    """--tenant a: only a's dropped samples, in the report and in the
    no_data envelope alike."""
    good = [[_NOW - d * 86400, "10"] for d in range(10, 0, -1)]
    mixed = _two_tenants(good[:-2] + [[_NOW - 7200, "NaN"], [_NOW - 3600, "NaN"]],
                         good[:-1] + [[_NOW - 3600, "+Inf"]])
    with _fake(mixed) as url:
        proc = _run("cardinality_forecasting", *_argv("cardinality_forecasting", url),
                    "--tenant", "a")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["summary"]["non_finite_samples_dropped"] == 2
    nan = [[_NOW - d * 3600, "NaN"] for d in range(3, 0, -1)]
    all_nan = _two_tenants(nan, nan + [[_NOW, "NaN"]])
    with _fake(all_nan) as url:
        proc = _run("cardinality_forecasting", *_argv("cardinality_forecasting", url),
                    "--tenant", "a")
    doc = json.loads(proc.stdout)
    assert (proc.returncode, doc["reason"]) == (2, "no_cardinality_data"), doc
    assert doc["summary"]["non_finite_samples_dropped"] == 3, doc["summary"]


@pytest.mark.parametrize("tool", ["alert_quality", "cardinality_forecasting"])
def test_markdown_discloses_non_finite(tool):
    if tool == "alert_quality":
        row = {"metric": {"alertname": "HostDown", "tenant": "tenant-one"},
               "values": [[_NOW - 120, "1"], [_NOW - 60, "NaN"]]}
    else:
        good = _GOOD_ROW[tool]
        row = dict(good, values=good["values"][:-1] + [[good["values"][-1][0], "NaN"]])
    body = {"status": "success", "data": {"resultType": "matrix", "result": [row]}}
    with _fake(body) as url:
        proc = _run(tool, "--prometheus", url, "--markdown")
    assert proc.returncode == 0, proc.stderr
    assert "1 non-finite sample value(s)" in proc.stdout, proc.stdout
