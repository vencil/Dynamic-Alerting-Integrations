#!/usr/bin/env python3
"""test_alert_quality.py — alert_quality.py pytest 風格測試。

驗證:
  1. compute_noise_score() — 震盪分數計算
  2. compute_stale_score() — 閒置分數計算
  3. compute_resolution_latency() — 解決延遲計算
  4. （#3212：suppression 這一維已移除，見 TestSuppressionIsGone）
  5. compute_overall_grade() — 綜合評級
  6. compute_tenant_score() — 租戶綜合分數
  7. generate_report() — 報告生成
  8. generate_markdown() — Markdown 輸出
  9. print_text_report() — 文字輸出
  10. CLI — 參數解析
  11. Prometheus / Alertmanager query — mock 測試
"""

import json
import sys
import time
from unittest.mock import patch

import pytest

import alert_quality as aq  # noqa: E402
from _lib_exitcodes import emit_result  # noqa: E402  (#3216: in-process main() → exit status)


# ── compute_noise_score ─────────────────────────────────────────

class TestComputeNoiseScore:
    """震盪分數計算測試。"""

    @pytest.mark.parametrize("fire_count,period_days,expected_grade", [
        (0, 30, "GOOD"),
        (5, 30, "GOOD"),
        (10, 30, "WARN"),
        (15, 30, "WARN"),
        (20, 30, "BAD"),
        (50, 30, "BAD"),
    ], ids=["zero", "low", "medium-boundary", "medium", "high-boundary", "very-high"])
    def test_grades(self, fire_count, period_days, expected_grade):
        """各種 fire count 正確評級。"""
        _, grade = aq.compute_noise_score(fire_count, period_days)
        assert grade == expected_grade

    def test_normalization_to_30d(self):
        """15 天 10 次 → 標準化為 30 天 20 次 → BAD。"""
        normalized, grade = aq.compute_noise_score(10, 15)
        assert normalized == 20
        assert grade == "BAD"

    def test_zero_period(self):
        """零天期間不應除零。"""
        count, grade = aq.compute_noise_score(5, 0)
        assert count == 5
        assert grade == "GOOD"


# ── compute_stale_score ─────────────────────────────────────────

class TestComputeStaleScore:
    """閒置分數計算測試。"""

    def test_recently_fired(self):
        """最近 fire 過 → GOOD。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 3600, now, 30)
        assert days < 1
        assert grade == "GOOD"

    def test_stale_alert(self):
        """超過 14 天未 fire → WARN。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 20 * 86400, now, 30)
        assert days >= 14
        assert grade == "WARN"

    def test_never_fired(self):
        """從未 fire（last_fired_ts=0） → WARN。"""
        now = time.time()
        days, grade = aq.compute_stale_score(0, now, 30)
        assert days == 30.0
        assert grade == "WARN"

    def test_just_under_threshold(self):
        """剛好 13 天 → GOOD。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 13 * 86400, now, 30)
        assert grade == "GOOD"


# ── compute_resolution_latency ──────────────────────────────────

class TestComputeResolutionLatency:
    """解決延遲計算測試。"""

    @pytest.mark.parametrize("durations,expected_grade", [
        ([], "GOOD"),
        ([3600, 7200, 1800], "GOOD"),
        ([60, 120, 90], "BAD"),
        ([100000, 200000], "WARN"),
    ], ids=["empty", "normal-range", "flapping", "slow-resolution"])
    def test_grades(self, durations, expected_grade):
        """各種解決延遲正確評級。"""
        _, grade = aq.compute_resolution_latency(durations)
        assert grade == expected_grade

    def test_avg_calculation(self):
        """平均值正確計算。"""
        avg, _ = aq.compute_resolution_latency([100, 200, 300])
        assert abs(avg - 200.0) < 0.01


# ── compute_overall_grade ──────────────────────────────────────

class TestComputeOverallGrade:
    """綜合評級測試。"""

    def _make_metrics(self, noise="GOOD", stale="GOOD", resolution="GOOD"):
        m = aq.AlertQualityMetrics(alertname="test", tenant="t")
        m.noise_grade = noise
        m.stale_grade = stale
        m.resolution_grade = resolution
        return m

    def test_all_good(self):
        """全 GOOD → GOOD。"""
        assert aq.compute_overall_grade(self._make_metrics()) == "GOOD"

    def test_one_warn(self):
        """一項 WARN → WARN。"""
        m = self._make_metrics(noise="WARN")
        assert aq.compute_overall_grade(m) == "WARN"

    def test_two_warn_becomes_bad(self):
        """兩項 WARN → BAD。"""
        m = self._make_metrics(noise="WARN", stale="WARN")
        assert aq.compute_overall_grade(m) == "BAD"

    def test_one_bad(self):
        """一項 BAD → BAD。"""
        m = self._make_metrics(resolution="BAD")
        assert aq.compute_overall_grade(m) == "BAD"


# ── compute_tenant_score ────────────────────────────────────────

class TestComputeTenantScore:
    """租戶綜合分數測試。"""

    def test_empty_list(self):
        """空清單 → 100 分。"""
        assert aq.compute_tenant_score([]) == 100.0

    def test_all_good(self):
        """全 GOOD → 100 分。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="GOOD"),
            aq.AlertQualityMetrics(alertname="b", tenant="t", overall_grade="GOOD"),
        ]
        assert aq.compute_tenant_score(alerts) == 100.0

    def test_mixed_grades(self):
        """混合評級正確加權平均。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="GOOD"),
            aq.AlertQualityMetrics(alertname="b", tenant="t", overall_grade="BAD"),
        ]
        # (100 + 0) / 2 = 50
        assert aq.compute_tenant_score(alerts) == 50.0

    def test_all_bad(self):
        """全 BAD → 0 分。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="BAD"),
        ]
        assert aq.compute_tenant_score(alerts) == 0.0


# ── generate_report ────────────────────────────────────────────

class TestGenerateReport:
    """報告生成測試。"""

    def test_empty_metrics(self):
        """空指標清單仍產生有效報告。"""
        report = aq.generate_report([], "30d")
        assert report.period == "30d"
        assert report.summary["total_tenants"] == 0
        assert report.summary["total_alertnames"] == 0

    def test_multi_tenant(self):
        """多租戶指標正確分組。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="HighConn", tenant="db-a", overall_grade="GOOD",
            ),
            aq.AlertQualityMetrics(
                alertname="HighCPU", tenant="db-b", overall_grade="WARN",
            ),
        ]
        report = aq.generate_report(metrics, "7d")
        assert report.summary["total_tenants"] == 2
        assert report.summary["good"] == 1
        assert report.summary["warn"] == 1

    def test_has_timestamp(self):
        """報告包含時間戳。"""
        report = aq.generate_report([], "30d")
        assert report.timestamp != ""

    def test_score_in_tenants(self):
        """每個 tenant 有 score 欄位。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        assert len(report.tenants) == 1
        assert report.tenants[0]["score"] == 100.0


# ── generate_markdown ──────────────────────────────────────────

class TestGenerateMarkdown:
    """Markdown 輸出測試。"""

    def test_contains_header(self):
        """Markdown 包含報告標題。"""
        report = aq.generate_report([], "30d")
        md = aq.generate_markdown(report)
        assert "## Alert Quality Report" in md

    def test_contains_table(self):
        """Markdown 包含表格。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        md = aq.generate_markdown(report)
        assert "| db-a |" in md

    def test_problem_details(self):
        """Markdown 包含問題警報詳細資訊。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="BadAlert", tenant="db-a",
                overall_grade="BAD", noise_grade="BAD",
                fire_count=30,
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        md = aq.generate_markdown(report)
        assert "`BadAlert`" in md
        assert "BAD" in md


# ── print_text_report ──────────────────────────────────────────

class TestPrintTextReport:
    """文字報告輸出測試。"""

    def test_contains_title(self, capsys):
        """報告包含標題。"""
        report = aq.generate_report([], "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "Alert Quality Report" in out or "警報品質評估報告" in out

    def test_shows_score(self, capsys):
        """報告顯示分數。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "100" in out

    def test_problem_alerts_shown(self, capsys):
        """問題警報顯示詳細資訊。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="NoisyAlert", tenant="db-a",
                overall_grade="BAD", noise_grade="BAD",
                fire_count=50,
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "NoisyAlert" in out
        assert "BAD" in out


# ── Prometheus query mock ──────────────────────────────────────
# W1: query_prometheus_alerts 的 fetch core 收斂進
# _lib_prometheus.prometheus_range，HTTP seam 改 patch
# _lib_prometheus.http_get_json。

class TestQueryPrometheusAlerts:
    """Prometheus 查詢 mock 測試。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳結果。"""
        def mock_get(url, timeout=10):
            return {
                "status": "success",
                "data": {"resultType": "matrix", "result": [{"metric": {"alertname": "X"}, "values": [[1, "1"]]}]},
            }, None
        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        result = aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)
        assert len(result) == 1

    def test_error_raises(self, monkeypatch):
        """HTTP 錯誤往上拋、帶錯誤原文（#3065：以前收斂成空清單）。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=10: (None, "refused"))
        with pytest.raises(aq.QueryError, match="refused"):
            aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)

    def test_api_error_raises(self, monkeypatch):
        """API 錯誤狀態往上拋（#3065）。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=10: ({"status": "error",
                                                      "error": "bad query"}, None))
        with pytest.raises(aq.QueryError, match="bad query"):
            aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)


# ── analyze_from_prometheus ────────────────────────────────────

class TestAnalyzeFromPrometheus:
    """端到端分析 mock 測試。"""

    def test_basic_analysis(self, monkeypatch):
        """基本分析流程。"""
        now = time.time()
        values = [
            [now - 600, "0"], [now - 540, "1"], [now - 480, "1"],
            [now - 420, "0"], [now - 360, "1"], [now - 300, "0"],
        ]
        result_data = {
            "status": "success",
            "data": {"resultType": "matrix", "result": [{
                "metric": {"alertname": "HighConn", "tenant": "db-a"},
                "values": values,
            }]},
        }
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: (result_data, None))
        metrics = aq.analyze_from_prometheus("http://prom", 86400)
        assert len(metrics) == 1
        assert metrics[0].alertname == "HighConn"
        assert metrics[0].tenant == "db-a"
        assert metrics[0].fire_count >= 1

    def test_no_data(self, monkeypatch):
        """查詢成功但無資料 → 空清單。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"resultType": "matrix", "result": []}}, None))
        metrics = aq.analyze_from_prometheus("http://prom", 86400)
        assert metrics == []

    def test_query_error_propagates(self, monkeypatch):
        """查詢失敗不再被當成無資料（#3065）。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: (None, "refused"))
        with pytest.raises(aq.QueryError):
            aq.analyze_from_prometheus("http://prom", 86400)


# ── CLI ────────────────────────────────────────────────────────

class TestCLI:
    """CLI 參數測試。"""

    def test_parser_defaults(self):
        """預設參數正確。"""
        parser = aq.build_parser()
        args = parser.parse_args(["--prometheus", "http://prom"])
        assert args.prometheus == "http://prom"
        assert args.period == "30d"
        assert args.json_output is False
        assert args.ci is False
        assert args.min_score == 0

    def test_all_flags(self):
        """所有 flag 正確解析。"""
        parser = aq.build_parser()
        args = parser.parse_args([
            "--prometheus", "http://prom",
            "--period", "7d",
            "--tenant", "db-a",
            "--json",
            "--ci",
            "--min-score", "80",
        ])
        assert args.period == "7d"
        assert args.tenant == "db-a"
        assert args.json_output is True
        assert args.ci is True
        assert args.min_score == 80


# ── #3212：suppression 這一維移除 ─────────────────────────────

class TestSuppressionIsGone:
    """#3212：suppression 從來沒量對過（AM v2 的 filter 比對 label，
    `alertstate=suppressed` 恆回空；分子是當下告警筆數、分母是期間樣本點數），
    整維移除：`--alertmanager` 是未知旗標（argparse，結束碼 2），報告不再有
    suppression 欄位，overall grade 只看三項。"""

    def test_alertmanager_flag_is_an_argparse_caller_error(self, capsys):
        with pytest.raises(SystemExit) as exc:
            aq.build_parser().parse_args(["--prometheus", "http://prom", "--alertmanager", "http://am"])
        assert exc.value.code == 2
        assert "--alertmanager" in capsys.readouterr().err

    def test_no_suppression_field_in_the_metrics(self):
        fields = set(aq.AlertQualityMetrics.__dataclass_fields__)
        assert not {f for f in fields if f.startswith("suppress")}, fields
        assert not hasattr(aq, "compute_suppression_ratio")
        assert not hasattr(aq, "query_alertmanager_alerts")

    def test_the_overall_grade_reads_three_dimensions(self):
        m = aq.AlertQualityMetrics(alertname="a", tenant="t")
        m.noise_grade = m.stale_grade = m.resolution_grade = "GOOD"
        m.suppression_grade = "WARN"  # not a field any more: must not count
        assert aq.compute_overall_grade(m) == "GOOD"

    def test_no_request_reaches_alertmanager(self, monkeypatch):
        seen = []
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: (seen.append(url) or (
                                {"status": "success", "data": {"resultType": "matrix", "result": [
                                    {"metric": {"alertname": "X", "tenant": "t"},
                                     "values": [[1, "1"], [2, "1"]]}]}}, None)))
        aq.analyze_from_prometheus("http://prom", 86400)
        assert seen and all("/api/v2/" not in u for u in seen), seen


# ── main() CLI entry ───────────────────────────────────────────

class TestMain:
    """main() CLI 進入點測試。"""

    def test_json_output(self, monkeypatch, capsys):
        """--json 輸出有效 JSON。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"resultType": "matrix", "result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--json",
        ])
        emit_result(aq.main())
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "summary" in data
        assert "tenants" in data

    def test_markdown_output(self, monkeypatch, capsys):
        """--markdown 輸出包含表格。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"resultType": "matrix", "result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--markdown",
        ])
        emit_result(aq.main())
        out = capsys.readouterr().out
        assert "## Alert Quality Report" in out

    def test_ci_mode_passes(self, monkeypatch):
        """CI 模式：無 BAD 時 exit code 0。"""
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"resultType": "matrix", "result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--ci",
        ])
        # 應該正常結束，不 raise
        emit_result(aq.main())

    @pytest.mark.parametrize("flags", [[], ["--json"]], ids=["text", "json"])
    def test_ci_mode_fails_on_bad(self, monkeypatch, flags):
        """CI 模式：有 BAD 時 exit code 1（#3216：--json 也是，status 隨 Report）。"""
        now = time.time()
        # 製造大量震盪 → BAD noise score
        values = []
        for i in range(100):
            values.append([now - (100 - i) * 60, str(i % 2)])  # 交替 0/1
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"resultType": "matrix", "result": [{
                                                          "metric": {"alertname": "Noisy", "tenant": "t"},
                                                          "values": values,
                                                      }]}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--ci", *flags,
        ])
        assert emit_result(aq.main()) == 1  # #3216: returned, as run() exits with it

    def test_invalid_period_exits(self, monkeypatch):
        """無效 period 字串應 exit 2 (caller error, #452)。"""
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--period", "xyz",
        ])
        with pytest.raises(SystemExit) as exc_info:
            emit_result(aq.main())
        assert exc_info.value.code == 2  # EXIT_CALLER_ERROR (#452: bad --period arg)

    def test_tenant_filter(self, monkeypatch, capsys):
        """--tenant 篩選傳遞到查詢。"""
        queries_made = []

        def mock_get(url, timeout=30):
            queries_made.append(url)
            return {"status": "success", "data": {"resultType": "matrix", "result": []}}, None

        monkeypatch.setattr("_lib_prometheus.http_get_json", mock_get)
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom",
            "--tenant", "db-a", "--json",
        ])
        emit_result(aq.main())
        assert any("db-a" in q for q in queries_made)


# ---------------------------------------------------------------------------
# #1779: the label-value whitelist is a full-string gate
# ---------------------------------------------------------------------------
class TestTenantWhitelistRejectsTrailingNewline:
    """`_TENANT_NAME_RE` is the PromQL / Alertmanager label-value injection
    whitelist for `--tenant`. Under `re.match` its `$` also succeeded before a
    trailing newline, so `"alpha\n"` passed and was interpolated into
    `{tenant="alpha\n"}` — the exact class the whitelist exists to stop.
    Three call sites, one gate: each is pinned with the same probe and an
    accept control so a gate that rejects everything cannot pass."""

    # #3073: a rejected tenant is a caller error, not an empty result.
    @staticmethod
    def _call(fn, tenant):
        try:
            fn(tenant)
        except aq.CallerError as exc:
            assert not isinstance(exc, aq.QueryError), exc
            assert "invalid tenant name" in str(exc), exc
            return False
        return True

    @pytest.mark.parametrize("tenant,accepted", [("alpha", True), ("alpha\n", False)])
    def test_prometheus_query_gate(self, monkeypatch, tenant, accepted):
        seen = []
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: (seen.append(url) or
                                                     ({"status": "success", "data": {"resultType": "matrix", "result": []}}, None)))
        ran = self._call(lambda t: aq.query_prometheus_alerts(
            "http://prom", "ALERTS", 3600, tenant=t), tenant)
        assert ran is accepted and bool(seen) is accepted, seen

    # ADR-035: the whitelist is the one tenant-id rule now. `Team_A` passed
    # the tool's own `^[a-zA-Z0-9_-]+$`; the shared DNS-1123 rule refuses it.
    @pytest.mark.parametrize("tenant", ["Team_A", "UPPER", "a" * 64])
    def test_gates_follow_the_shared_tenant_id_rule(self, monkeypatch, capsys, tenant):
        seen = []
        monkeypatch.setattr("_lib_prometheus.http_get_json",
                            lambda url, timeout=30: (seen.append(url) or
                                                     ({"status": "success", "data": {"resultType": "matrix", "result": []}}, None)))
        assert not self._call(lambda t: aq.query_prometheus_alerts(
            "http://prom", "ALERTS", 3600, tenant=t), tenant)
        assert not seen, seen
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--tenant", tenant,
        ])
        with pytest.raises(SystemExit) as exc_info:
            emit_result(aq.main())
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "invalid tenant name" in err and "DNS-1123" in err, err

    def test_cli_gate_rejects_trailing_newline(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--tenant", "alpha\n",
        ])
        with pytest.raises(SystemExit) as exc_info:
            emit_result(aq.main())
        assert exc_info.value.code == 2
        assert "invalid tenant name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# #3065: a Prometheus query error is a caller error, not "no alerts"
# ---------------------------------------------------------------------------
import contextlib  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402

_AQ_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "scripts", "tools", "ops", "alert_quality.py")


@contextlib.contextmanager
def _fake_prometheus(mode):
    """本機假 Prometheus：``mode="503"`` 時 query_range 回 503 + error JSON；
    ``mode="firing"`` 回一筆剛 firing 的 ALERTS series（對照組）。"""
    now = time.time()

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            if mode == "503":
                code, body = 503, {"status": "error", "errorType": "unavailable",
                                   "error": "query queue full"}
            elif self.path.startswith("/api/v1/query_range"):
                code, body = 200, {"status": "success", "data": {
                    "resultType": "matrix",
                    "result": [{
                        "metric": {"alertname": "HostDown", "tenant": "tenant-one",
                                   "alertstate": "firing"},
                        "values": [[now - 120, "1"], [now - 60, "1"]],
                    }]}}
            else:
                code, body = 404, {"status": "error", "error": "not found"}
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


def _run_aq(url, *extra):
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["NO_PROXY"] = env["no_proxy"] = "*"
    env["DA_LANG"] = "en"
    return subprocess.run(
        [sys.executable, _AQ_SCRIPT, "--prometheus", url, *extra],
        capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)


class TestQueryErrorIsCallerError:
    """#3065：query_range 失敗以前被收斂成「沒有警報」— 報告全 GOOD、
    `--ci` rc 0。現在 rc 2，stderr 帶錯誤原文，`--json` 吐 caller_error envelope。"""

    @pytest.mark.parametrize(
        "extra", [[], ["--ci"], ["--markdown"], ["--markdown", "--ci"]],
        ids=["plain", "ci", "markdown", "markdown-ci"])
    def test_503_text_exits_caller_error(self, extra):
        with _fake_prometheus("503") as url:
            proc = _run_aq(url, *extra)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        # 不出報告：stdout 不得有任何（空的、全 GOOD 的）報告
        assert proc.stdout == "", proc.stdout
        assert "503" in proc.stderr, proc.stderr
        assert "Traceback" not in proc.stderr, proc.stderr

    @pytest.mark.parametrize("extra", [[], ["--ci"]], ids=["plain", "ci"])
    def test_503_json_emits_caller_error_envelope(self, extra):
        with _fake_prometheus("503") as url:
            proc = _run_aq(url, "--json", *extra)
        assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
        doc = json.loads(proc.stdout)
        assert doc["status"] == "caller_error"
        assert doc["reason"] == "prometheus_query_error"
        assert "503" in doc["error"], doc
        # 沿用正常 schema 的鍵、歸零
        assert doc["tenants"] == []
        assert doc["summary"]["total_alertnames"] == 0
        assert doc["period"] == "30d"
        happy_keys = set(aq.asdict(aq.generate_report([], "30d")))
        assert set(doc) == happy_keys | {"status", "reason", "error"}

    def test_firing_series_is_reported_as_before(self):
        with _fake_prometheus("firing") as url:
            proc = _run_aq(url, "--json", "--ci")
        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        doc = json.loads(proc.stdout)
        assert "status" not in doc
        assert doc["summary"]["total_alertnames"] == 1
        assert doc["tenants"][0]["alerts"][0]["alertname"] == "HostDown"
