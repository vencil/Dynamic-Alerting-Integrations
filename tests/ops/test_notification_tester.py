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

import contextlib
import http.server
import json
import os
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from factories import make_receiver, make_routing_config, make_tenant_yaml, write_yaml  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# receiver slots — taken from the route generator's verdict (#3076)
# ═══════════════════════════════════════════════════════════════════════
def _slots(tenant, config):
    """(label, status) per slot of a dry run, in report order."""
    report = nt.test_tenant_receivers(tenant, config, dry_run=True, rate_limit=0)
    return [(r.receiver_name, r.status) for r in report.receivers]


class TestReceiverSlots:
    """Receiver 清單取自路由產生器的逐項判定（取代先前的 extract_receivers）。"""

    def test_no_routing(self):
        """無 _routing 區塊的 tenant 沒有任何項目。"""
        assert _slots("db-a", {"mysql_connections": 100}) == []

    def test_routing_not_dict(self):
        """_routing 不是 dict 時沒有任何項目。"""
        assert _slots("db-a", {"_routing": "invalid"}) == []

    def test_single_webhook_receiver(self):
        config = {"_routing": {"receiver": {"type": "webhook",
                                            "url": "https://hooks.example.com/alert"}}}
        report = nt.test_tenant_receivers("db-a", config, dry_run=True, rate_limit=0)
        assert [(r.receiver_type, r.url_tested) for r in report.receivers] == [
            ("webhook", "https://hooks.example.com/alert")]

    def test_main_plus_overrides(self):
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://main.example.com"},
                "overrides": [
                    {"alertname": "CriticalAlert",
                     "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/T/B/X"}},
                    {"metric_group": "disk",
                     "receiver": {"type": "pagerduty", "service_key": "key123"}},
                ],
            },
        }
        report = nt.test_tenant_receivers("db-a", config, dry_run=True, rate_limit=0)
        assert [r.receiver_type for r in report.receivers] == ["webhook", "slack", "pagerduty"]

    def test_routes_receivers_follow_overrides(self):
        """ADR-007 `routes` 排在 overrides 之後（#2245）；產生器不產出的項目照列、不測。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://main.example.com"},
                "overrides": [
                    {"alertname": "A", "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/T/B/X"}},
                ],
                "routes": [
                    {"match": {"severity": "critical"},
                     "receiver": {"type": "pagerduty", "service_key": "k"}},
                    {"match": {"severity": "warning"}},  # 無 receiver
                    "not-a-mapping",
                ],
            },
        }
        assert _slots("tenant-x", config) == [
            ("tenant-x-main", nt.STATUS_DRY_RUN),
            ("tenant-x-override-0", nt.STATUS_DRY_RUN),
            ("tenant-x-route-0", nt.STATUS_DRY_RUN),
            ("tenant-x-route-1", nt.STATUS_INVALID_CONFIG),
            ("tenant-x-route-2", nt.STATUS_NOT_RENDERED)]

    def test_override_without_receiver(self):
        """缺 receiver 的 override 不送出，報 INVALID_CONFIG（#3076 前直接不列）。"""
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://main.example.com"},
                "overrides": [{"alertname": "SomeAlert"}],
            },
        }
        assert _slots("db-a", config) == [("db-a-main", nt.STATUS_DRY_RUN),
                                          ("db-a-override-0", nt.STATUS_INVALID_CONFIG)]

    def test_receiver_missing_type(self):
        """缺 type 的主 receiver 不送出，報 INVALID_CONFIG（#3076 前直接不列）。"""
        config = {"_routing": {"receiver": {"url": "https://no-type.example.com"}}}
        assert _slots("db-a", config) == [("db-a-main", nt.STATUS_INVALID_CONFIG)]

    def test_label_assignment(self):
        config = {
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://example.com"},
                "overrides": [
                    {"alertname": "A",
                     "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
                ],
            },
        }
        labels = [label for label, _ in _slots("tenant-x", config)]
        assert labels == ["tenant-x-main", "tenant-x-override-0"]

    @pytest.mark.parametrize("slot,label", [
        ("receiver", "t-main"), ("overrides[3]", "t-override-3"),
        ("routes[0]", "t-route-0"), ("overrides", "t-overrides"),
        ("routes", "t-routes")])
    def test_slot_label(self, slot, label):
        assert nt._slot_label("t", slot) == label


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
        ("webhook", {"type": "webhook", "url": "ftp://bad.example.com"},
         "not in the format tenant-config.schema.json requires"),
        ("webhook", {"type": "webhook", "url": ""}, "missing required field"),
        ("slack", {"type": "slack"}, "missing required field"),
        ("webhook", {"type": "webhook", "url": "not-a-url"},
         "not in the format tenant-config.schema.json requires"),
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
        assert "field 'smarthost' value 'smtp.example.com' is not in the format" in err


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
        """連線拒絕應返回 CONNECTION_REFUSED（依 reason 的型別判斷，#2995）。"""
        mock_urlopen.side_effect = urllib.error.URLError(
            ConnectionRefusedError(111, "Connection refused"))
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "down"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_CONNECTION_REFUSED

    @patch("notification_tester.urllib.request.urlopen")
    def test_os_error(self, mock_urlopen):
        """其他 OSError 應返回 CONNECTION_ERROR，不是 CONNECTION_REFUSED（#2995）。"""
        mock_urlopen.side_effect = OSError("Network unreachable")
        recv = {"type": "webhook", "url": "https://hooks.example.com", "_label": "net-err"}
        result = nt.test_receiver(recv)
        assert result.status == nt.STATUS_CONNECTION_ERROR


# ═══════════════════════════════════════════════════════════════════════
# test_receiver — 讀取階段逾時分類（#2965）
# ═══════════════════════════════════════════════════════════════════════
class _SlowHandler(http.server.BaseHTTPRequestHandler):
    """收到請求後等待（最多約 1 秒）才回 200；teardown 時由 release 提早放行。"""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler 的介面名稱
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.release.wait(self.server.delay)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            pass  # client 已因逾時斷線

    def log_message(self, *args):
        pass


@contextlib.contextmanager
def _slow_server(delay, tls_context=None, port=0):
    """以 *port*（預設 0 號）起本機慢 server，回傳 URL；離開時關閉。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), _SlowHandler)
    srv.daemon_threads = True
    srv.delay = delay
    srv.release = threading.Event()
    scheme = "http"
    if tls_context is not None:
        srv.socket = tls_context.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{scheme}://127.0.0.1:{srv.server_address[1]}/hook"
    finally:
        srv.release.set()
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture
def _no_proxy(monkeypatch):
    """urllib 會讀 *_proxy 環境變數；本機慢 server 必須直連。"""
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    # 已快取的 opener 內 ProxyHandler 是依建立當時的環境變數組的
    monkeypatch.setattr(urllib.request, "_opener", None)


def _make_self_signed(tmp_path):
    """產生 127.0.0.1 的自簽憑證；回傳 (server SSLContext, 憑證路徑)。"""
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl not available")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    proc = subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "1",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
    )
    if proc.returncode != 0:
        pytest.skip(f"openssl req failed: {proc.stderr.strip()[:200]}")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    return server_ctx, cert


@pytest.fixture
def _self_signed_tls(tmp_path, monkeypatch):
    """產生 127.0.0.1 的自簽憑證；client 端以它為信任根（不關閉驗證）。"""
    server_ctx, cert = _make_self_signed(tmp_path)
    real_factory = ssl.create_default_context

    def _client_ctx(*args, **kwargs):
        ctx = real_factory(*args, **kwargs)
        ctx.load_verify_locations(cafile=str(cert))
        return ctx

    monkeypatch.setattr(ssl, "_create_default_https_context", _client_ctx)
    # urlopen 快取全域 opener，其 HTTPSHandler 的 context 在建立時就固定；
    # 清掉讓本測試重建（離開時 monkeypatch 還原）。
    monkeypatch.setattr(urllib.request, "_opener", None)
    return server_ctx


@pytest.mark.usefixtures("_no_proxy")
class TestReadTimeoutClassification:
    """連線成功但回應遲到：urllib 不把讀取階段的 TimeoutError 包成 URLError（#2965）。"""

    def _recv(self, url):
        return {"type": "webhook", "url": url, "_label": "slow"}

    def test_slow_http_server_read_timeout_is_timeout(self):
        with _slow_server(delay=1.0) as url:
            result = nt.test_receiver(self._recv(url), timeout=0.2)
        assert result.status == nt.STATUS_TIMEOUT, result.detail

    def test_slow_http_server_with_enough_timeout_is_ok(self):
        with _slow_server(delay=0.3) as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_OK, result.detail

    def test_slow_https_server_read_timeout_is_timeout(self, _self_signed_tls):
        with _slow_server(delay=1.0, tls_context=_self_signed_tls) as url:
            result = nt.test_receiver(self._recv(url), timeout=0.2)
        assert result.status == nt.STATUS_TIMEOUT, result.detail

    def test_slow_https_server_with_enough_timeout_is_ok(self, _self_signed_tls):
        """對照組：證明 https 那筆 TIMEOUT 不是憑證設定錯誤造成的。"""
        with _slow_server(delay=0.3, tls_context=_self_signed_tls) as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_OK, result.detail

    @patch("notification_tester.urllib.request.urlopen")
    def test_urlerror_wrapping_empty_timeouterror_is_timeout(self, mock_urlopen):
        """reason 是訊息為空的 TimeoutError：字串比對抓不到，要靠型別。"""
        mock_urlopen.side_effect = urllib.error.URLError(TimeoutError())
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_TIMEOUT
        assert result.detail == "timed out"  # 不留空白 detail 給 operator

    @patch("notification_tester.urllib.request.urlopen")
    def test_bare_empty_timeouterror_is_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError()
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_TIMEOUT
        assert result.detail == "timed out"

    @patch("notification_tester.urllib.request.urlopen")
    def test_connection_reset_is_not_timeout(self, mock_urlopen):
        """非逾時的裸 OSError 不得被放寬成 TIMEOUT（分類見 TestConnectionErrorClassification）。"""
        mock_urlopen.side_effect = ConnectionResetError("reset by peer")
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_CONNECTION_ERROR

    @pytest.mark.parametrize("reason, expected", [
        (ConnectionRefusedError(111, "Connection refused"), "CONNECTION_REFUSED"),
        (socket.gaierror(-2, "Name or service not known"), "CONNECTION_ERROR"),
    ])
    @patch("notification_tester.urllib.request.urlopen")
    def test_urlerror_wrapping_non_timeout_oserror_is_not_timeout(
            self, mock_urlopen, reason, expected):
        """URLError 包的是非逾時的 OSError 物件時，型別判斷不得放寬成 TIMEOUT。"""
        mock_urlopen.side_effect = urllib.error.URLError(reason)
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == expected


@contextlib.contextmanager
def _raw_server(reply):
    """收下整個請求後回 ``reply`` 原始位元組並關線；回傳 URL。

    請求 body 必須讀完、關線前先 shutdown 寫端：接收緩衝區還有沒讀的位元組
    時 close 會送 RST 而不是 FIN，client 偶爾讀到 Connection reset 而不是
    reply 本身。
    """
    srv = socket.create_server(("127.0.0.1", 0))
    srv.settimeout(5)

    def _serve():
        with contextlib.suppress(OSError):
            conn, _ = srv.accept()
            with conn:
                conn.settimeout(5)
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                head, _, body = buf.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    name, _, value = line.partition(b":")
                    if name.strip().lower() == b"content-length":
                        length = int(value.strip())
                while len(body) < length:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    body += chunk
                conn.sendall(reply)
                conn.shutdown(socket.SHUT_WR)
                while conn.recv(4096):
                    pass

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}/hook"
    finally:
        thread.join(timeout=5)
        srv.close()


@pytest.mark.usefixtures("_no_proxy")
class TestMalformedResponse:
    """receiver 回了東西但不是合格的 HTTP 回應：http.client.HTTPException
    不是 OSError，先前沒人接住，整支工具 traceback（#2995 第 2 類）。"""

    def _recv(self, url):
        return {"type": "webhook", "url": url, "_label": "broken"}

    @pytest.mark.parametrize("reply, exc_name", [
        (b"GARBAGE\r\n\r\n", "BadStatusLine"),
        (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort", "IncompleteRead"),
    ], ids=["bad-status-line", "truncated-body"])
    def test_malformed_response_is_http_error(self, reply, exc_name):
        with _raw_server(reply) as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_HTTP_ERROR, result.detail
        assert result.detail.startswith(f"malformed response: {exc_name}")

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:abc/hook",
        "http://127.0.0.1:9/ho ok",
    ], ids=["non-numeric-port", "space-in-path"])
    def test_invalid_url_is_not_a_malformed_response(self, url):
        """InvalidURL 也是 HTTPException，但在送出請求前就丟出：是 URL 錯，
        不是 receiver 回了壞回應。"""
        result = nt.test_receiver(self._recv(url), timeout=2)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        # #3048：路由產生器不收的 URL，送出前就以產生器自己那句判定。
        assert result.detail.startswith("receiver type 'webhook' field 'url' value"), result.detail
        assert "not in the format tenant-config.schema.json requires" in result.detail

    def test_close_without_response_stays_connection_error(self):
        """RemoteDisconnected 同時是 OSError 與 BadStatusLine：維持連線錯誤的
        標籤，不被 HTTPException 分支搶走。"""
        with _raw_server(b"") as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_CONNECTION_ERROR, result.detail


@contextlib.contextmanager
def _reset_server():
    """讀完整個請求後以 SO_LINGER 0 關線，送出 RST：client 端拿到裸的
    ConnectionResetError（urllib 不包成 URLError）。回傳 URL。"""
    srv = socket.create_server(("127.0.0.1", 0))
    srv.settimeout(5)

    def _serve():
        with contextlib.suppress(OSError):
            conn, _ = srv.accept()
            conn.settimeout(5)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, body = buf.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            while len(body) < length:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                body += chunk
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("ii", 1, 0))
            conn.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}/hook"
    finally:
        thread.join(timeout=5)
        srv.close()


def _closed_port():
    """取一個剛釋放、沒有人在聽的本機 port。"""
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


@pytest.mark.usefixtures("_no_proxy")
class TestConnectionErrorClassification:
    """連線階段錯誤依例外型別分類，CONNECTION_REFUSED 只留給真的被拒絕連線
    （#2995 第 1 類）。"""

    def _recv(self, url):
        return {"type": "webhook", "url": url, "_label": "conn"}

    # ── 真 socket ──────────────────────────────────────────────────────
    def test_real_refused_connection_is_connection_refused(self):
        result = nt.test_receiver(
            self._recv(f"http://127.0.0.1:{_closed_port()}/hook"), timeout=5)
        assert result.status == nt.STATUS_CONNECTION_REFUSED, result.detail

    def test_real_connection_reset_is_connection_error(self):
        with _reset_server() as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_CONNECTION_ERROR, result.detail

    def test_https_to_plain_http_server_is_tls_error(self):
        """https 連到純 http 埠：URLError(SSLError)。"""
        with _slow_server(delay=0) as url:
            result = nt.test_receiver(
                self._recv(url.replace("http://", "https://", 1)), timeout=5)
        assert result.status == nt.STATUS_TLS_ERROR, result.detail

    def test_untrusted_certificate_is_tls_error(self, tmp_path):
        """自簽憑證、client 不信任：URLError(SSLCertVerificationError)。"""
        server_ctx, _ = _make_self_signed(tmp_path)
        with _slow_server(delay=0, tls_context=server_ctx) as url:
            result = nt.test_receiver(self._recv(url), timeout=5)
        assert result.status == nt.STATUS_TLS_ERROR, result.detail
        assert len(result.detail) <= 80  # owner：80 字上限不放寬

    @patch("notification_tester.urllib.request.urlopen")
    def test_proxy_timeout_status_is_the_proxy_not_a_timeout(self, mock_urlopen):
        """proxy 回 504 Connection Timed Out：失敗的是 proxy 這一段，
        不是 receiver 逾時。"""
        mock_urlopen.side_effect = urllib.error.URLError(
            OSError("Tunnel connection failed: 504 Connection Timed Out"))
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_CONNECTION_ERROR
        assert result.detail.startswith("via proxy: ")

    # ── 實務上造不出的情況：mock urlopen ──────────────────────────────
    @patch("notification_tester.urllib.request.urlopen")
    def test_dns_failure_is_connection_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(
            socket.gaierror(-2, "Name or service not known"))
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_CONNECTION_ERROR
        assert "Name or service not known" in result.detail

    @patch("notification_tester.urllib.request.urlopen")
    def test_proxy_tunnel_failure_is_marked_via_proxy(self, mock_urlopen):
        """走 HTTPS proxy 時，DNS 失敗與拒絕連線都變成 Tunnel connection failed：
        測到的是 proxy，不是 receiver。"""
        mock_urlopen.side_effect = urllib.error.URLError(
            OSError("Tunnel connection failed: 502 Bad Gateway"))
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_CONNECTION_ERROR
        assert result.detail == "via proxy: Tunnel connection failed: 502 Bad Gateway"

    @patch("notification_tester.urllib.request.urlopen")
    def test_proxy_detail_is_still_capped_at_80(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError(
            OSError("Tunnel connection failed: 502 " + "x" * 200))
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.detail.startswith("via proxy: Tunnel connection failed")
        assert len(result.detail) == 80

    @patch("notification_tester.urllib.request.urlopen")
    def test_string_reason_is_not_matched_on_text(self, mock_urlopen):
        """reason 不是例外物件時不比對訊息字串：urllib 真的拒絕連線一定給
        ConnectionRefusedError 物件，字串 reason 只會是 URL 本身的問題。"""
        mock_urlopen.side_effect = urllib.error.URLError(reason="Connection refused")
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == nt.STATUS_CONNECTION_ERROR

    @pytest.mark.parametrize("exc, expected", [
        (ConnectionRefusedError(111, "Connection refused"), "CONNECTION_REFUSED"),
        (ssl.SSLError(1, "[SSL] record layer failure"), "TLS_ERROR"),
    ], ids=["refused", "ssl"])
    @patch("notification_tester.urllib.request.urlopen")
    def test_bare_oserror_uses_the_same_classification(self, mock_urlopen, exc, expected):
        """沒被 urllib 包成 URLError 的 OSError 走同一套分類。"""
        mock_urlopen.side_effect = exc
        result = nt.test_receiver(self._recv("https://hooks.example.com"))
        assert result.status == expected

    def test_every_status_has_a_symbol(self):
        """新狀態沒登記會在文字報告裡顯示成 ?。"""
        statuses = {v for k, v in vars(nt).items() if k.startswith("STATUS_")}
        assert statuses - set(nt._STATUS_SYMBOLS) == set()

    @pytest.mark.parametrize("status", ["CONNECTION_ERROR", "TLS_ERROR"])
    def test_new_statuses_count_as_failed(self, status):
        """結束碼不變：只有 OK／DRY_RUN 算通過。"""
        fake = nt.ReceiverTestResult(receiver_name="r", receiver_type="webhook",
                                     status=status)
        recv = make_receiver("webhook", url="https://hooks.example.com")
        with patch("notification_tester.test_receiver", return_value=fake):
            report = nt.test_tenant_receivers("t", {"_routing": {"receiver": recv}},
                                              rate_limit=0)
        assert (report.passed, report.failed, report.skipped) == (0, 1, 0)


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

    @pytest.mark.parametrize("rtype", ["Webhook", "WEBHOOK", " webhook", "webhook "])
    def test_type_is_matched_exactly(self, rtype):
        """type 不做大小寫或前後空白正規化，與路由產生器、schema `const` 及 Go guard 一致（#2180）。"""
        recv = {"type": rtype, "url": "https://hooks.example.com/alert", "_label": "t"}
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_CONFIG
        assert "unknown receiver type" in result.detail
        _, err = nt.validate_receiver_url(recv)
        assert err is not None and "unknown receiver type" in err

    @pytest.mark.parametrize("rtype", [{"a": 1}, ["webhook"], 5, 0, False, {}, []],
                             ids=["mapping", "list", "int", "zero", "false",
                                  "empty-mapping", "empty-list"])
    def test_non_string_type_is_invalid_config(self, rtype):
        """#2839：type 不是字串時回 INVALID_CONFIG 並點名，不得 traceback（同 _grar_merge.receiver_type()）。"""
        recv = {"type": rtype, "url": "http://127.0.0.1:9/", "_label": "t-main"}
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_CONFIG
        assert "receiver type must be a string" in result.detail
        assert result.receiver_type == ""
        _, err = nt.validate_receiver_url(recv)
        assert err is not None and "receiver type must be a string" in err

    @pytest.mark.parametrize("keys,expect", [
        ({"routing_key": "r"}, None),
        ({}, "none is set"),
        ({"service_key": "k", "routing_key": "r"}, "not both"),
        ({"service_key": 0}, "field 'service_key' must be a string"),
        ({"service_key": [], "routing_key": "r"}, "field 'service_key' must be a string"),
    ], ids=["routing-key-only", "neither", "both", "int-key", "list-key-with-rk"])
    def test_pagerduty_exactly_one_key(self, keys, expect):
        """PagerDuty service_key／routing_key 恰好一個（#2137）；合法者走到 dry-run。"""
        recv = {"type": "pagerduty", "_label": "pd", **keys}
        result = nt.test_receiver(recv, dry_run=True)
        if expect is None:
            assert result.status == nt.STATUS_DRY_RUN
        else:
            assert result.status == nt.STATUS_INVALID_CONFIG
            assert expect in result.detail

    def test_email_skipped(self):
        """Email receiver 目前應返回 SKIPPED（SMTP 測試尚未實作）。"""
        recv = {"type": "email", "to": "admin@example.com", "smarthost": "smtp.example.com:587", "from": "alerts@example.com", "_label": "mail"}
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
                    {"alertname": "A",
                     "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
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
                    {"alertname": "A",
                     "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}},
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

    def test_receiver_from_routing_defaults_and_tenant_placeholder(self, tmp_path):
        """#2752：receiver 取自路由產生器解析後的 _routing（含 _routing_defaults 與 {{tenant}} 代換）。"""
        write_yaml(str(tmp_path), "_defaults.yaml",
                   "defaults:\n  mysql_connections: 80\n"
                   "_routing_defaults:\n  receiver:\n    type: webhook\n"
                   "    url: \"https://hooks.example.com/{{tenant}}\"\n")
        write_yaml(str(tmp_path), "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: 5\n")
        reports = nt.run_all_tests(str(tmp_path), dry_run=True, rate_limit=0)
        assert [r.tenant for r in reports] == ["db-a"]
        assert reports[0].receivers[0].url_tested == "https://hooks.example.com/db-a"

    @pytest.mark.parametrize("rtype", [{"a": 1}, ["webhook"], 5, 0, False, {}, []],
                             ids=["mapping", "list", "int", "zero", "false",
                                  "empty-mapping", "empty-list"])
    @pytest.mark.parametrize("where", ["receiver", "overrides", "routes"])
    def test_non_string_type_reported_per_tenant(self, tmp_path, rtype, where):
        """#2839：任一位置的 receiver.type 非字串時，該租戶報一筆 INVALID_CONFIG，其他租戶照常測試。"""
        ok = {"type": "webhook", "url": "https://ok.example.com"}
        bad_recv = {"type": rtype, "url": "http://127.0.0.1:9/"}
        if where == "receiver":
            routing = {"receiver": bad_recv}
        elif where == "overrides":
            routing = {"receiver": ok,
                       "overrides": [{"alertname": "X", "receiver": bad_recv}]}
        else:
            routing = {"receiver": ok,
                       "routes": [{"match": {"severity": "critical"},
                                   "receiver": bad_recv}]}
        write_yaml(str(tmp_path), "db-bad.yaml", make_tenant_yaml(
            "db-bad", keys={"mysql_connections": 5}, routing=routing))
        write_yaml(str(tmp_path), "db-ok.yaml", make_tenant_yaml(
            "db-ok", keys={"mysql_connections": 5}, routing={"receiver": ok}))
        reports = nt.run_all_tests(str(tmp_path), dry_run=True, rate_limit=0)
        by_tenant = {r.tenant: r for r in reports}
        assert by_tenant["db-ok"].passed == 1 and by_tenant["db-ok"].failed == 0
        bad = [r for r in by_tenant["db-bad"].receivers
               if r.status == nt.STATUS_INVALID_CONFIG]
        assert len(bad) == 1 and "receiver type must be a string" in bad[0].detail
        assert by_tenant["db-bad"].failed == 1
        nt.format_text_report(reports)
        nt.format_json_report(reports)

    def test_refused_tree_raises(self, tmp_path):
        """#2752：路由產生器拒收的樹（同一租戶宣告於兩檔）不得回空清單。"""
        body = make_tenant_yaml("db-a", routing={"receiver": {"type": "webhook", "url": "https://a.com"}})
        write_yaml(str(tmp_path), "db-a.yaml", body)
        write_yaml(str(tmp_path), "db-a-copy.yaml", body)
        with pytest.raises(nt.RoutingTreeRefused) as exc_info:
            nt.run_all_tests(str(tmp_path), dry_run=True)
        assert any("more than one file" in line for line in exc_info.value.lines)


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
        """不存在的 config-dir 應 exit caller error。"""
        from _lib_exitcodes import CallerError  # rc 2 at run()'s exit (#3276)
        with patch("sys.argv", ["notification_tester.py", "--config-dir", "/nonexistent/path"]):
            with pytest.raises(CallerError) as exc_info:
                nt.main()
            assert exc_info.value.reason == "dir_not_found"

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

    @pytest.mark.parametrize("value", ["0", "-1", "nan"])
    def test_timeout_must_be_above_zero(self, tmp_path, capsys, value):
        """#2957: 0 reported a healthy receiver as CONNECTION_REFUSED and a
        negative value crashed; both are refused before any request."""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
            "db-a", routing={"receiver": {"type": "webhook",
                                          "url": "https://hooks.example.com"}}))
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--timeout", value]):
            with patch("notification_tester.urllib.request.urlopen") as mock_urlopen:
                with pytest.raises(SystemExit) as exc_info:
                    nt.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        assert "--timeout" in capsys.readouterr().err
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc"])
    def test_rate_limit_must_be_zero_or_above(self, tmp_path, capsys, value):
        """#2966: a negative pause was skipped silently by the ``> 0`` guard
        and ``inf`` raised OverflowError in time.sleep after the first
        receiver had been tested; all are refused before any request."""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
            "db-a", routing={"receiver": {"type": "webhook",
                                          "url": "https://hooks.example.com"}}))
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--rate-limit", value]):
            with patch("notification_tester.urllib.request.urlopen") as mock_urlopen:
                with pytest.raises(SystemExit) as exc_info:
                    nt.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        assert "--rate-limit" in capsys.readouterr().err
        mock_urlopen.assert_not_called()

    @pytest.mark.parametrize("value, expected", [("0", 0.0), ("0.25", 0.25)])
    def test_rate_limit_zero_and_fraction_reach_the_tester(self, tmp_path, value, expected):
        """Control for the test above: 0 keeps its "do not wait" meaning."""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
            "db-a", routing={"receiver": {"type": "webhook",
                                          "url": "https://hooks.example.com"}}))
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--rate-limit", value]):
            with patch("notification_tester.run_all_tests",
                       return_value=[]) as mock_all:
                try:
                    nt.main()
                except SystemExit as e:
                    assert e.code in (0, None)
        assert mock_all.call_args.kwargs["rate_limit"] == expected

    def test_fractional_timeout_reaches_urlopen(self, tmp_path):
        """Control for the test above: a value above 0 is accepted as is."""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
            "db-a", routing={"receiver": {"type": "webhook",
                                          "url": "https://hooks.example.com"}}))
        resp = MagicMock()
        resp.read.return_value = b'{}'
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--rate-limit", "0", "--timeout", "0.5"]):
            with patch("notification_tester.urllib.request.urlopen",
                       return_value=resp) as mock_urlopen:
                try:
                    nt.main()
                except SystemExit as e:
                    assert e.code in (0, None)
        assert mock_urlopen.call_args.kwargs["timeout"] == 0.5

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

    def test_refused_tree_exits_caller_error(self, tmp_path, capsys):
        """#2752：路由產生器拒收的樹 → rc 2，JSON 標 caller_error，不是「沒有 receiver」。"""
        body = make_tenant_yaml("db-a", routing={"receiver": {"type": "webhook", "url": "https://a.com"}})
        write_yaml(str(tmp_path), "db-a.yaml", body)
        write_yaml(str(tmp_path), "db-a-copy.yaml", body)
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path),
                                 "--json", "--dry-run"]):
            with pytest.raises(SystemExit) as exc_info:
                nt.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "caller_error"
        assert data["reason"] == "routing_tree_refused"
        assert data["summary"]["total_receivers"] == 0
        assert "route generator refuses this tree" in captured.err

    def test_refusal_lines_are_escaped(self, tmp_path, capsys):
        """#2752：轉述產生器拒收訊息時，控制字元須經 safe_label 中和。"""
        with patch("sys.argv", ["notification_tester.py", "--config-dir", str(tmp_path), "--dry-run"]):
            with patch.object(nt, "run_all_tests",
                              side_effect=nt.RoutingTreeRefused(["FAIL: tenant \x1b[31mred"])):
                with pytest.raises(SystemExit) as exc_info:
                    nt.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "\x1b" not in err
        assert "FAIL: tenant" in err


# ═══════════════════════════════════════════════════════════════════════
# #3048：receiver 合格與否採路由產生器的判定；port 範圍；主機名編碼
# ═══════════════════════════════════════════════════════════════════════
_LONG_LABEL_URL = "http://" + "a" * 64 + ".example.com/hook"   # IDNA：label too long
_EMPTY_LABEL_URL = "http://a..b/hook"                         # IDNA：empty label
_FOLDED_PORT = 99999
_FOLDED_TO = _FOLDED_PORT - 65536                              # glibc 折算後的 port


def _webhook(url, label="w"):
    return {"type": "webhook", "url": url, "_label": label}


def _email(label="mail", **fields):
    recv = {"type": "email", "to": "admin@example.com",
            "smarthost": "smtp.example.com:587", "from": "alerts@example.com",
            "_label": label}
    recv.update(fields)
    return recv


@pytest.mark.usefixtures("_no_proxy")
class TestGeneratorVerdict:
    """receiver 本身的設定被產生器（build_receiver_config）拒收的，不得被
    tester 拿去測或報 DRY_RUN（路由層的略過見 test_notification_tester_render_ledger.py，#3076）；壞主機名、
    超出範圍的 port 標 INVALID_URL，不得 traceback 或被折算到別的 port。"""

    @pytest.mark.parametrize("url", [_LONG_LABEL_URL, _EMPTY_LABEL_URL],
                             ids=["64-char-label", "empty-label"])
    def test_host_encoding_error_is_invalid_url_when_sent(self, url):
        result = nt.test_receiver(_webhook(url), timeout=2)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert result.detail.startswith("invalid URL:"), result.detail
        assert "idna" in result.detail, result.detail

    @pytest.mark.parametrize("url", [_LONG_LABEL_URL, _EMPTY_LABEL_URL],
                             ids=["64-char-label", "empty-label"])
    def test_dry_run_does_not_claim_hostname_encoding_checked(self, url):
        """dry-run 不做 IDNA 預檢（會誤擋 percent-encoded 主機名與 proxy），
        detail 如實寫出只在送出時檢查。"""
        result = nt.test_receiver(_webhook(url), dry_run=True)
        assert result.status == nt.STATUS_DRY_RUN
        assert "hostname encoding is checked only when a request is sent" in result.detail
        assert "dry-run" in result.detail

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_unclosed_ipv6_bracket_is_refused_like_the_generator(self, dry_run):
        result = nt.test_receiver(_webhook("http://[::1/hook"), timeout=2, dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert result.detail == ("receiver type 'webhook' field 'url' value "
                                 "'http://[::1/hook' is not in the format "
                                 "tenant-config.schema.json requires")

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_bracketed_non_ip_host_is_invalid_url(self, dry_run):
        """`[1:2]` 合 schema pattern（產生器收），但 urlsplit 的括號主機檢查丟
        ValueError；要在送出前標 INVALID_URL，不得 traceback。"""
        result = nt.test_receiver(_webhook("http://[1:2]/hook"), timeout=2, dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert result.detail.startswith("invalid URL 'http://[1:2]/hook':"), result.detail

    @pytest.mark.parametrize("port", ["99999", "0", "65536"])
    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_port_out_of_range_is_invalid_url(self, port, dry_run):
        result = nt.test_receiver(_webhook(f"http://127.0.0.1:{port}/hook"),
                                  timeout=2, dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert "port must be from 1 to 65535" in result.detail

    def test_port_at_range_edges_is_accepted(self):
        for port in ("1", "65535"):
            result = nt.test_receiver(_webhook(f"http://127.0.0.1:{port}/hook"), dry_run=True)
            assert result.status == nt.STATUS_DRY_RUN, (port, result.detail)

    def test_folded_port_with_a_listener_is_not_ok(self):
        """glibc 把 99999 折成 99999-65536；那個 port 剛好有服務時，修前報 OK。"""
        try:
            server = _slow_server(delay=0, port=_FOLDED_TO)
            served_url = server.__enter__()
        except OSError as exc:
            pytest.skip(f"127.0.0.1:{_FOLDED_TO} is in use, cannot stand in for the folded port: {exc}")
        try:
            assert served_url.endswith(f":{_FOLDED_TO}/hook")
            result = nt.test_receiver(
                _webhook(f"http://127.0.0.1:{_FOLDED_PORT}/hook"), timeout=5)
        finally:
            server.__exit__(None, None, None)
        assert result.status == nt.STATUS_INVALID_URL, result.detail

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_email_empty_recipient_is_invalid_config(self, dry_run):
        result = nt.test_receiver(_email(to=[""]), dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_CONFIG, result.detail
        assert result.detail == ("receiver type 'email' field 'to' item 0 must be "
                                 "a non-empty string, got ''")

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_email_smarthost_non_numeric_port_is_invalid_url(self, dry_run):
        result = nt.test_receiver(_email(smarthost="smtp.example.com:abc"), dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert "field 'smarthost' value 'smtp.example.com:abc'" in result.detail

    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_email_smarthost_port_out_of_range_is_invalid_url(self, dry_run):
        result = nt.test_receiver(_email(smarthost="smtp.example.com:99999"), dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        assert "port must be from 1 to 65535" in result.detail

    def test_generator_refusal_of_an_optional_field_is_invalid_config(self):
        recv = {**_webhook("https://hooks.example.com/alert"), "send_resolved": "maybe"}
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_CONFIG, result.detail
        assert "send_resolved" in result.detail

    _EMPTY_URL_CASES = [
        ({"type": "webhook", "url": "", "_label": "w"}, "url"),
        ({"type": "webhook", "url": None, "_label": "w"}, "url"),
        ({"type": "webhook", "_label": "w"}, "url"),
        ({"type": "slack", "api_url": "", "_label": "s"}, "api_url"),
        ({"type": "slack", "api_url": None, "_label": "s"}, "api_url"),
        (_email(smarthost=""), "smarthost"),
        (_email(smarthost=None), "smarthost"),
        ({k: v for k, v in _email().items() if k != "smarthost"}, "smarthost"),
    ]
    _EMPTY_URL_IDS = ["webhook-empty", "webhook-null", "webhook-missing",
                      "slack-empty", "slack-null",
                      "email-smarthost-empty", "email-smarthost-null",
                      "email-smarthost-missing"]

    @pytest.mark.parametrize("recv,field", _EMPTY_URL_CASES, ids=_EMPTY_URL_IDS)
    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_unset_url_field_is_invalid_config(self, recv, field, dry_run):
        """缺鍵、空字串、null 都是「沒給」（receiver_field_state 為 FIELD_UNSET），
        與修前相同標 INVALID_CONFIG；產生器對 "" / null 標 invalid_receiver_field，
        不得因此被歸成 INVALID_URL。"""
        result = nt.test_receiver(dict(recv), timeout=2, dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_CONFIG, result.detail
        assert f"requires '{field}'" in result.detail, result.detail

    @pytest.mark.parametrize("recv", [
        _webhook("ftp://hooks.example.com/alert"),
        {"type": "slack", "api_url": "not a url", "_label": "s"},
        _email(smarthost="smtp.example.com"),
        _webhook(5),
    ], ids=["webhook-scheme", "slack-format", "email-no-port", "webhook-not-string"])
    @pytest.mark.parametrize("dry_run", [False, True], ids=["live", "dry-run"])
    def test_malformed_url_field_is_invalid_url(self, recv, dry_run):
        result = nt.test_receiver(dict(recv), timeout=2, dry_run=dry_run)
        assert result.status == nt.STATUS_INVALID_URL, result.detail

    @pytest.mark.parametrize("recv", [
        _webhook("https://hooks.example.com/alert"),
        _webhook("http://[::1/hook"),
        _webhook("http://h/a b"),
        {**_webhook("https://hooks.example.com/alert"), "send_resolved": "maybe"},
        _email(),
        _email(to=[""]),
        _email(smarthost="smtp.example.com"),
        {"type": "email", "to": "a@b.c", "smarthost": "smtp.example.com:25", "_label": "m"},
        {"type": "pagerduty", "_label": "pd"},
        {"type": "slack", "_label": "s"},
    ], ids=["ok", "ipv6", "space", "optional", "email-ok", "email-empty-to",
            "email-no-port", "email-no-from", "pd-no-key", "slack-no-url"])
    def test_verdict_matches_the_generator(self, recv):
        """產生器收 ⇔ dry-run 不報 INVALID_*（產生器不看 tester 自加的 _label）。"""
        from _grar_merge import build_receiver_config
        am_config, _ = build_receiver_config(
            {k: v for k, v in recv.items() if k != "_label"}, "t")
        result = nt.test_receiver(recv, dry_run=True)
        refused = result.status in (nt.STATUS_INVALID_CONFIG, nt.STATUS_INVALID_URL)
        assert refused == (am_config is None), result

    def test_generator_detail_is_escaped_in_text_report(self):
        recv = _webhook("http://h/\x1b[31mred")
        result = nt.test_receiver(recv, dry_run=True)
        assert result.status == nt.STATUS_INVALID_URL, result.detail
        report = nt.TenantTestReport(tenant="t", receivers=[result], failed=1)
        assert "\x1b" not in nt.format_text_report([report])

    def test_one_bad_receiver_does_not_lose_the_others(self, tmp_path):
        """修前：主機名編碼錯誤 traceback，同一輪其他 receiver 的結果全丟。"""
        with _slow_server(delay=0) as good_url:
            write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
                "db-a", keys={"mysql_connections": 5},
                routing={"receiver": {"type": "webhook", "url": _LONG_LABEL_URL},
                         "routes": [{"match": {"severity": "critical"},
                                     "receiver": {"type": "webhook", "url": good_url}}]}))
            write_yaml(str(tmp_path), "db-b.yaml", make_tenant_yaml(
                "db-b", keys={"mysql_connections": 5},
                routing={"receiver": {"type": "webhook", "url": good_url}}))
            reports = nt.run_all_tests(str(tmp_path), timeout=5, rate_limit=0)
        statuses = {r.receiver_name: r.status for rep in reports for r in rep.receivers}
        assert statuses == {"db-a-main": nt.STATUS_INVALID_URL,
                            "db-a-route-0": nt.STATUS_OK,
                            "db-b-main": nt.STATUS_OK}, statuses


class TestCLIExitCodes3048:
    """#3048：真實 CLI 的結束碼（owner 已同意的變化）。"""

    _SCRIPT = os.path.join(_TOOLS_DIR, "ops", "notification_tester.py")

    @staticmethod
    def _env():
        env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
        env["no_proxy"] = env["NO_PROXY"] = "*"
        return env

    def _run(self, conf_dir, *flags):
        proc = subprocess.run(
            [sys.executable, self._SCRIPT, "--config-dir", str(conf_dir), "--json",
             "--rate-limit", "0", "--timeout", "5", *flags],
            capture_output=True, text=True, encoding="utf-8", timeout=60, env=self._env())
        return proc

    def _tree(self, tmp_path, bad_receiver, good_url):
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml(
            "db-a", keys={"mysql_connections": 5}, routing={"receiver": bad_receiver}))
        write_yaml(str(tmp_path), "db-b.yaml", make_tenant_yaml(
            "db-b", keys={"mysql_connections": 5},
            routing={"receiver": {"type": "webhook", "url": good_url}}))

    def test_host_encoding_error_reports_instead_of_crashing(self, tmp_path):
        with _slow_server(delay=0) as good_url:
            self._tree(tmp_path, {"type": "webhook", "url": _LONG_LABEL_URL}, good_url)
            plain = self._run(tmp_path)
            ci = self._run(tmp_path, "--ci")
        assert plain.returncode == 0, plain.stderr
        assert ci.returncode == 1, ci.stderr
        for proc in (plain, ci):
            assert "Traceback" not in proc.stderr
            data = json.loads(proc.stdout)
            statuses = {r["receiver_name"]: r["status"]
                        for t in data["tenants"] for r in t["receivers"]}
            assert statuses == {"db-a-main": "INVALID_URL", "db-b-main": "OK"}, statuses

    @pytest.mark.parametrize("bad", [
        {"type": "email", "to": [""], "smarthost": "smtp.example.com:587",
         "from": "alerts@example.com"},
        {"type": "webhook", "url": "http://127.0.0.1:99999/hook"},
        {"type": "webhook", "url": ""},
        {"type": "webhook", "url": None},
        {"type": "webhook", "url": "ftp://hooks.example.com/alert"},
    ], ids=["email-empty-to", "port-out-of-range", "url-empty", "url-null",
            "url-malformed"])
    def test_dry_run_ci_fails_on_what_the_generator_refuses(self, tmp_path, bad):
        self._tree(tmp_path, bad, "https://hooks.example.com/alert")
        proc = self._run(tmp_path, "--dry-run", "--ci")
        assert proc.returncode == 1, (proc.stdout, proc.stderr)
        data = json.loads(proc.stdout)
        assert data["summary"]["failed"] == 1 and data["summary"]["passed"] == 1


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
