"""test_notification_tester.py — notification_tester.py 的單元測試。

測試涵蓋：
  - Receiver 提取（單一 / 多個 / override / 缺失）
  - URL 驗證（各 receiver type 的合法與非法 URL）
  - 乾跑模式（不發送實際請求）
  - HTTP 測試（mock success / failure / timeout / auth error）
  - Rate limiting 驗證
  - CI 模式 exit code
  - JSON / Text 輸出格式
  - Edge cases（空 config、未知 receiver type、缺少必填欄位）
"""

import json
import os
import sys
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

# Ensure import path
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "scripts", "tools")
for _p in [_TOOLS_DIR, os.path.join(_TOOLS_DIR, "ops")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import notification_tester as nt  # noqa: E402
from factories import make_receiver, make_routing_config, make_tenant_yaml, write_yaml  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# extract_receivers
# ═══════════════════════════════════════════════════════════════════════
class TestExtractReceivers:
    """Receiver 提取邏輯測試。"""

    def test_no_routing(self):
        """無 _routing 區塊的 tenant 應返回空列表。"""
        result = nt.extract_receivers("db-a", {"mysql_connections": 100})
        assert result == []

    def test_routing_not_dict(self):
        """_routing 不是 dict 時應返回空列表。"""
        result = nt.extract_receivers("db-a", {"_routing": "invalid"})
        assert result == []

    def test_single_webhook_receiver(self):
        """提取單一 webhook receiver。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
            },
        }
        result = nt.extract_receivers("db-a", config)
        assert len(result) == 1
        assert result[0]["type"] == "webhook"
        assert result[0]["url"] == "https://hooks.example.com/alert"

    def test_main_plus_overrides(self):
        """提取主 receiver 加上 override receivers。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://main.example.com"},
                "overrides": [
                    {"match": "CriticalAlert", "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/T/B/X"}},
                    {"match": "DiskFull", "receiver": {"type": "pagerduty", "service_key": "key123"}},
                ],
            },
        }
        result = nt.extract_receivers("db-a", config)
        assert len(result) == 3
        types = [r["type"] for r in result]
        assert types == ["webhook", "slack", "pagerduty"]

    def test_override_without_receiver(self):
        """Override 項目若缺少 receiver 應跳過。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://main.example.com"},
                "overrides": [
                    {"match": "SomeAlert"},  # no receiver
                ],
            },
        }
        result = nt.extract_receivers("db-a", config)
        assert len(result) == 1

    def test_receiver_missing_type(self):
        """receiver 缺少 type 欄位應跳過。"""
        config = {
            "_routing": {
                "receiver": {"url": "https://no-type.example.com"},
            },
        }
        result = nt.extract_receivers("db-a", config)
        assert result == []

    def test_label_assignment(self):
        """確認 _label 自動指派。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://example.com"},
                "overrides": [
                    {"receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
                ],
            },
        }
        result = nt.extract_receivers("tenant-x", config)
        assert result[0]["_label"] == "tenant-x-main"
        assert result[1]["_label"] == "tenant-x-override-0"


# ═══════════════════════════════════════════════════════════════════════
# validate_receiver_url
# ═══════════════════════════════════════════════════════════════════════
class TestValidateReceiverUrl:
    """URL 驗證邏輯測試。"""

    @pytest.mark.parametrize("rtype,fields,expected_ok", [
        ("webhook", {"type": "webhook", "url": "https://hooks.example.com/alert"}, True),
        ("slack", {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/X"}, True),
        ("teams", {"type": "teams", "webhook_url": "https://outlook.office.com/webhook/test"}, True),
    ], ids=["webhook-valid", "slack-valid", "teams-valid"])
    def test_valid_urls(self, rtype, fields, expected_ok):
        """合法 URL 應通過驗證。"""
        url, err = nt.validate_receiver_url(fields)
        assert err is None
        assert url is not None

    @pytest.mark.parametrize("rtype,fields,expected_err_contains", [
        ("webhook", {"type": "webhook", "url": "ftp://bad.example.com"}, "invalid URL scheme"),
        ("webhook", {"type": "webhook", "url": ""}, "missing required field"),
        ("slack", {"type": "slack"}, "missing required field"),
        ("webhook", {"type": "webhook", "url": "not-a-url"}, "invalid URL scheme"),
    ], ids=["ftp-scheme", "empty-url", "missing-field", "no-scheme"])
    def test_invalid_urls(self, rtype, fields, expected_err_contains):
        """非法 URL 應返回錯誤訊息。"""
        url, err = nt.validate_receiver_url(fields)
        assert err is not None
        assert expected_err_contains in err

    def test_unknown_receiver_type(self):
        """未知 receiver type 應返回錯誤。"""
        url, err = nt.validate_receiver_url({"type": "carrier_pigeon"})
        assert err is not None
        assert "unknown receiver type" in err

    def test_pagerduty_no_url_field(self):
        """PagerDuty 沒有 URL 欄位，應返回 (None, None)。"""
        url, err = nt.validate_receiver_url({"type": "pagerduty", "service_key": "abc"})
        assert url is None
        assert err is None

    def test_email_smarthost_valid(self):
        """Email smarthost host:port 格式應通過。"""
        url, err = nt.validate_receiver_url({"type": "email", "to": "a@b.com", "smarthost": "smtp.example.com:587"})
        assert err is None
        assert url == "smtp.example.com:587"

    def test_email_smarthost_invalid(self):
        """Email smarthost 缺少 port 應報錯。"""
        url, err = nt.validate_receiver_url({"type": "email", "to": "a@b.com", "smarthost": "smtp.example.com"})
        assert err is not None
        assert "host:port" in err


# ═══════════════════════════════════════════════════════════════════════
# test_receiver — dry-run mode
# ═══════════════════════════════════════════════════════════════════════
class TestDryRun:
    """乾跑模式測試 — 不應發送任何 HTTP 請求。"""

    def test_webhook_dry_run(self):
        """Webhook dry-run 應返回 DRY_RUN 狀態。"""
        recv = {"type": "webhook", "url": "https://hooks.example.com/alert", "_label": "test"}
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_DRY_RUN
        assert "dry-run" in result.detail.lower()

    def test_invalid_url_in_dry_run(self):
        """Dry-run 也應攔截無效 URL。"""
        recv = {"type": "webhook", "url": "ftp://bad.com", "_label": "test"}
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_URL

    def test_missing_required_field_in_dry_run(self):
        """Dry-run 也應驗證必填欄位。"""
        recv = {"type": "slack", "_label": "test"}  # missing api_url
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_CONFIG

    @patch("notification_tester.urllib.request.urlopen")
    def test_dry_run_no_http_call(self, mock_urlopen):
        """Dry-run 模式不應呼叫 urlopen。"""
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "test"}
        nt.test_receiver(recv, dry_run=True)
        mock_urlopen.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# test_receiver — HTTP tests (mocked)
# ═══════════════════════════════════════════════════════════════════════
class TestHttpTests:
    """HTTP 連通性測試（mock urlopen）。"""

    def _mock_urlopen_success(self, *args, **kwargs):
        """模擬成功的 HTTP 回應。"""
        resp = MagicMock()
        resp.read.return_value = b'{"ok": true}'
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("notification_tester.urllib.request.urlopen")
    def test_webhook_success(self, mock_urlopen):
        """Webhook 成功應返回 OK。"""
        mock_urlopen.side_effect = self._mock_urlopen_success
        recv = {"type": "webhook", "url": "https://hooks.example.com/alert", "_label": "noc"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_OK
        assert result.latency_ms >= 0
        assert "200" in result.detail

    @patch("notification_tester.urllib.request.urlopen")
    def test_slack_success(self, mock_urlopen):
        """Slack webhook 成功測試。"""
        mock_urlopen.side_effect = self._mock_urlopen_success
        recv = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/X", "_label": "slack"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_OK

    @patch("notification_tester.urllib.request.urlopen")
    def test_teams_success(self, mock_urlopen):
        """Teams webhook 成功測試。"""
        mock_urlopen.side_effect = self._mock_urlopen_success
        recv = {"type": "teams", "webhook_url": "https://outlook.office.com/webhook/test", "_label": "teams"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_OK

    @patch("notification_tester.urllib.request.urlopen")
    def test_pagerduty_success(self, mock_urlopen):
        """PagerDuty Events API 成功測試。"""
        mock_urlopen.side_effect = self._mock_urlopen_success
        recv = {"type": "pagerduty", "service_key": "test-key", "_label": "pd"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_OK

    @patch("notification_tester.urllib.request.urlopen")
    def test_http_401_auth_error(self, mock_urlopen):
        """HTTP 401 應返回 AUTH_ERROR。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://hooks.example.com", code=401, msg="Unauthorized",
            hdrs=None, fp=None,
        )
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "auth"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_AUTH_ERROR
        assert "401" in result.detail

    @patch("notification_tester.urllib.request.urlopen")
    def test_http_403_auth_error(self, mock_urlopen):
        """HTTP 403 應返回 AUTH_ERROR。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://hooks.example.com", code=403, msg="Forbidden",
            hdrs=None, fp=None,
        )
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "forbidden"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_AUTH_ERROR

    @patch("notification_tester.urllib.request.urlopen")
    def test_http_500_error(self, mock_urlopen):
        """HTTP 500 應返回 HTTP_ERROR。"""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://hooks.example.com", code=500, msg="Internal Server Error",
            hdrs=None, fp=None,
        )
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "server-err"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_HTTP_ERROR
        assert "500" in result.detail

    @patch("notification_tester.urllib.request.urlopen")
    def test_timeout(self, mock_urlopen):
        """連線逾時應返回 TIMEOUT。"""
        mock_urlopen.side_effect = urllib.error.URLError(reason="timed out")
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "slow"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_TIMEOUT

    @patch("notification_tester.urllib.request.urlopen")
    def test_connection_refused(self, mock_urlopen):
        """連線拒絕應返回 CONNECTION_REFUSED。"""
        mock_urlopen.side_effect = urllib.error.URLError(reason="Connection refused")
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "down"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_CONNECTION_REFUSED

    @patch("notification_tester.urllib.request.urlopen")
    def test_os_error(self, mock_urlopen):
        """OSError 應返回 CONNECTION_REFUSED。"""
        mock_urlopen.side_effect = OSError("Network unreachable")
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "net-err"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_CONNECTION_REFUSED


# ═══════════════════════════════════════════════════════════════════════
# test_receiver — edge cases
# ═══════════════════════════════════════════════════════════════════════
class TestEdgeCases:
    """邊界條件測試。"""

    def test_unknown_receiver_type(self):
        """未知 receiver type 應返回 INVALID_CONFIG。"""
        recv = {"type": "carrier_pigeon", "_label": "pigeon"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_INVALID_CONFIG

    def test_email_skipped(self):
        """Email receiver 目前應返回 SKIPPED（SMTP 測試尚未實作）。"""
        recv = {"type": "email", "to": "admin@example.com", "smarthost": "smtp.example.com:587", "_label": "mail"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_SKIPPED

    def test_rocketchat_treated_as_webhook(self):
        """Rocket.Chat 應使用 webhook payload 測試。"""
        recv = {"type": "rocketchat", "url": "https://chat.example.com/hooks/abc", "_label": "rc"}
        with patch("notification_tester.urllib.request.urlopen") as mock:
            resp = MagicMock()
            resp.read.return_value = b'{"success": true}'
            resp.status = 200
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            mock.return_value = resp
            result = nt.test_receiver(recv)
            assert result.status == nt.STATUS_OK


# ═══════════════════════════════════════════════════════════════════════
# test_tenant_receivers — orchestration
# ═══════════════════════════════════════════════════════════════════════
class TestTenantOrchestration:
    """租戶級別測試編排。"""

    def test_tenant_no_receivers(self):
        """無 receiver 的租戶應返回空結果。"""
        report = nt.test_tenant_receivers("db-a", {"mysql_connections": 100})
        assert report.tenant == "db-a"
        assert report.receivers == []
        assert report.passed == 0

    @patch("notification_tester.urllib.request.urlopen")
    def test_tenant_multiple_receivers(self, mock_urlopen):
        """多個 receiver 應全部被測試。"""
        resp = MagicMock()
        resp.read.return_value = b'{}'
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
                "overrides": [
                    {"receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
                ],
            },
        }
        report = nt.test_tenant_receivers("db-a", config, rate_limit=0)
        assert len(report.receivers) == 2
        assert report.passed == 2

    @patch("notification_tester.time.sleep")
    @patch("notification_tester.urllib.request.urlopen")
    def test_rate_limiting(self, mock_urlopen, mock_sleep):
        """Rate limiting 應在請求之間等待。"""
        resp = MagicMock()
        resp.read.return_value = b'{}'
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp

        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
                "overrides": [
                    {"receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
                ],
            },
        }
        nt.test_tenant_receivers("db-a", config, rate_limit=1.0)
        # sleep should be called once (between first and second receiver)
        mock_sleep.assert_called_once_with(1.0)

    def test_dry_run_orchestration(self):
        """Dry-run 應對所有 receiver 返回 DRY_RUN。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
            },
        }
        report = nt.test_tenant_receivers("db-a", config, dry_run=True, rate_limit=0)
        assert report.passed == 1
        assert report.receivers[0].status == nt.STATUS_DRY_RUN


# ═══════════════════════════════════════════════════════════════════════
# run_all_tests — full pipeline with config-dir
# ═══════════════════════════════════════════════════════════════════════
class TestRunAllTests:
    """完整管線測試（使用 tmp_path 作為 config-dir）。"""

    def test_empty_config_dir(self, tmp_path):
        """空配置目錄應返回空結果。"""
        reports = nt.run_all_tests(str(tmp_path), dry_run=True)
        assert reports == []

    def test_tenant_without_routing(self, tmp_path):
        """無 routing 的租戶不應出現在結果中。"""
        yaml_content = "tenants:\n  db-a:\n    mysql_connections: 100\n"
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)
        reports = nt.run_all_tests(str(tmp_path), dry_run=True)
        assert reports == []

    def test_tenant_with_webhook(self, tmp_path):
        """有 webhook receiver 的租戶應被測試。"""
        yaml_content = make_tenant_yaml(
            "db-a",
            keys={"mysql_connections": 100},
            routing={"receiver": {"type": "webhook", "url": "https://hooks.example.com"}},
        )
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)
        reports = nt.run_all_tests(str(tmp_path), dry_run=True, rate_limit=0)
        assert len(reports) == 1
        assert reports[0].tenant == "db-a"
        assert reports[0].passed == 1

    def test_tenant_filter(self, tmp_path):
        """--tenant 過濾器應只測試指定租戶。"""
        for name in ("db-a", "db-b"):
            yaml_content = make_tenant_yaml(
                name,
                routing={"receiver": {"type": "webhook", "url": f"https://{name}.example.com"}},
            )
            write_yaml(str(tmp_path), f"{name}.yaml", yaml_content)
        reports = nt.run_all_tests(str(tmp_path), tenant_filter="db-b", dry_run=True, rate_limit=0)
        assert len(reports) == 1
        assert reports[0].tenant == "db-b"

    def test_tenant_filter_nonexistent(self, tmp_path):
        """過濾不存在的租戶應返回空結果。"""
        yaml_content = make_tenant_yaml("db-a", routing={"receiver": {"type": "webhook", "url": "https://a.com"}})
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)
        reports = nt.run_all_tests(str(tmp_path), tenant_filter="db-z", dry_run=True)
        assert reports == []


# ═══════════════════════════════════════════════════════════════════════
# Output formatting
# ═══════════════════════════════════════════════════════════════════════
class TestOutputFormatting:
    """輸出格式化測試。"""

    def _make_sample_reports(self):
        """建立測試用 report 資料。"""
        return [
            nt.TenantTestReport(
                tenant="db-a",
                receivers=[
                    nt.ReceiverTestResult("noc-hook", "webhook", nt.STATUS_OK, 120, "HTTP 200"),
                    nt.ReceiverTestResult("dba-pg", "pagerduty", nt.STATUS_OK, 340, "HTTP 202"),
                    nt.ReceiverTestResult("team-ch", "slack", nt.STATUS_TIMEOUT, 5000, "timed out"),
                ],
                passed=2, failed=1, skipped=0,
            ),
        ]

    def test_text_format_contains_tenant(self):
        """Text 輸出應包含租戶名稱。"""
        reports = self._make_sample_reports()
        text = nt.format_text_report(reports)
        assert "db-a" in text
        assert "noc-hook" in text
        assert "FAIL" in text

    def test_text_format_empty(self):
        """空結果應顯示提示訊息。"""
        text = nt.format_text_report([])
        assert len(text) > 0

    def test_json_format_structure(self):
        """JSON 輸出應包含正確結構。"""
        reports = self._make_sample_reports()
        raw = nt.format_json_report(reports)
        data = json.loads(raw)
        assert data["tool"] == "test-notification"
        assert data["status"] == "fail"  # because one receiver failed
        assert data["summary"]["total_receivers"] == 3
        assert data["summary"]["passed"] == 2
        assert data["summary"]["failed"] == 1

    def test_json_format_pass_status(self):
        """全部成功時 JSON status 應為 pass。"""
        reports = [
            nt.TenantTestReport(
                tenant="db-a",
                receivers=[nt.ReceiverTestResult("hook", "webhook", nt.STATUS_OK, 100, "OK")],
                passed=1, failed=0, skipped=0,
            ),
        ]
        data = json.loads(nt.format_json_report(reports))
        assert data["status"] == "pass"


# ═══════════════════════════════════════════════════════════════════════
# CLI main() tests
# ═══════════════════════════════════════════════════════════════════════
class TestCLI:
    """CLI 入口點測試。"""

    def test_missing_config_dir(self, tmp_path):
        """不存在的 config-dir 應 exit 1。"""
        with patch("sys.argv", ["notification_tester.py", "--config-dir", "/nonexistent/path"]):
            with pytest.raises(SystemExit) as exc_info:
                nt.main()
            assert exc_info.value.code == 1

    def test_ci_mode_exit_code_on_failure(self, tmp_path):
        """CI 模式下有失敗 receiver 應 exit 1。"""
        yaml_content = make_tenant_yaml(
            "db-a",
            routing={"receiver": {"type": "webhook", "url": "https://hooks.example.com"}},
        )
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)

        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path), "--ci"]):
            with patch("notification_tester.urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = urllib.error.URLError(reason="Connection refused")
                with pytest.raises(SystemExit) as exc_info:
                    nt.main()
                assert exc_info.value.code == 1

    def test_ci_mode_exit_code_on_success(self, tmp_path):
        """CI 模式下全部成功不應 exit 1。"""
        yaml_content = make_tenant_yaml(
            "db-a",
            routing={"receiver": {"type": "webhook", "url": "https://hooks.example.com"}},
        )
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)

        resp = MagicMock()
        resp.read.return_value = b'{}'
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)

        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--ci", "--rate-limit", "0"]):
            with patch("notification_tester.urllib.request.urlopen", return_value=resp):
                # Should not raise SystemExit (or exit with 0)
                try:
                    nt.main()
                except SystemExit as e:
                    assert e.code == 0 or e.code is None

    def test_dry_run_cli(self, tmp_path):
        """CLI --dry-run 應正常完成。"""
        yaml_content = make_tenant_yaml(
            "db-a",
            routing={"receiver": {"type": "webhook", "url": "https://hooks.example.com"}},
        )
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)

        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path), "--dry-run"]):
            # Should not raise
            nt.main()

    def test_json_output_cli(self, tmp_path, capsys):
        """CLI --json 應輸出合法 JSON。"""
        yaml_content = make_tenant_yaml(
            "db-a",
            routing={"receiver": {"type": "webhook", "url": "https://hooks.example.com"}},
        )
        write_yaml(str(tmp_path), "db-a.yaml", yaml_content)

        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--json", "--dry-run"]):
            nt.main()
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["tool"] == "test-notification"


# ═══════════════════════════════════════════════════════════════════════
# Payload builders
# ═══════════════════════════════════════════════════════════════════════
class TestPayloadBuilders:
    """測試各 payload builder 產生合法 JSON。"""

    def test_webhook_payload(self):
        """Webhook payload 應為合法 JSON，包含 alerts 陣列。"""
        payload = nt._build_webhook_payload()
        data = json.loads(payload)
        assert "alerts" in data
        assert data["alerts"][0]["labels"]["alertname"] == "DynamicAlertingNotificationTest"

    def test_slack_payload(self):
        """Slack payload 應包含 text 欄位。"""
        payload = nt._build_slack_payload()
        data = json.loads(payload)
        assert "text" in data

    def test_teams_payload(self):
        """Teams payload 應包含 Adaptive Card。"""
        payload = nt._build_teams_payload()
        data = json.loads(payload)
        assert data["type"] == "message"
        assert len(data["attachments"]) > 0

    def test_pagerduty_payload(self):
        """PagerDuty payload 應包含 routing_key 和 event_action。"""
        payload = nt._build_pagerduty_payload("test-key")
        data = json.loads(payload)
        assert data["routing_key"] == "test-key"
        assert data["event_action"] == "trigger"
