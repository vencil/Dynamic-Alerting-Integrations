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


# ── alert_quality: Alertmanager (#3073) ──────────────────────────────

def _aq(url, am_url, *extra):
    return _run("alert_quality", "--prometheus", url, "--alertmanager", am_url,
                "--json", "--ci", *extra)


def test_alertmanager_answered_is_measured():
    """Control: 30 suppressed alerts are counted (ratio 0.6 → WARN), rc 0."""
    with _fake(_good("alert_quality"), am=("ok", 30)) as url:
        proc = _aq(url, url)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    alert = doc["tenants"][0]["alerts"][0]
    assert alert["suppressed_count"] == 30 and alert["suppression_grade"] == "WARN", alert


@pytest.mark.parametrize("am", [
    ("refused",), ("500",), ("body", {"alerts": []}), ("body", ["x"]),
    ("body", [{"labels": "x"}]),
], ids=["refused", "http-500", "object-body", "item-not-object", "labels-not-object"])
@pytest.mark.parametrize("extra", [[], ["--min-score", "75"]], ids=["ci", "ci-min-score"])
def test_alertmanager_not_measured_is_rc2(am, extra):
    """#3073: an asked-for Alertmanager that did not answer is not "0
    suppressed" — rc 2 with ``reason: alertmanager_query_error``, also
    where ``--min-score`` used to let the run through (rc 0)."""
    with _fake(_good("alert_quality"), am=am if am[0] != "refused" else None) as url:
        am_url = "http://127.0.0.1:1" if am[0] == "refused" else url
        proc = _aq(url, am_url, *extra)
    assert proc.returncode == 2, (am, proc.returncode, proc.stdout, proc.stderr)
    doc = json.loads(proc.stdout)
    assert (doc["status"], doc["reason"]) == ("caller_error", "alertmanager_query_error"), doc
    assert doc["tenants"] == [], doc
    assert "Alertmanager query failed" in proc.stderr, proc.stderr


@pytest.mark.parametrize("extra", [[], ["--markdown"]], ids=["text", "markdown"])
def test_alertmanager_not_measured_text_modes_print_no_report(extra):
    with _fake(_good("alert_quality")) as url:
        proc = _run("alert_quality", "--prometheus", url,
                    "--alertmanager", "http://127.0.0.1:1", *extra)
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout == "", proc.stdout
    assert "Alertmanager query failed" in proc.stderr, proc.stderr
