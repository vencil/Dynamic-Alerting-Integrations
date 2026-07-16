"""_lib_prometheus.probe_health 單元測試（da-tools ROI r3 W2）。

probe_health 收斂了 5 個手抄探活站（federation_check ×2 / byo_check ×2 /
shadow_verify ×1）。手抄站原本**沒有** scheme 檢查——lib 其他 HTTP helper
都先過 `_validate_url_scheme`、唯獨探活路徑裸奔的 SSRF 防護落差；收斂後
scheme 驗證帶進探活路徑，本檔釘住這條新防線 + 雙態回傳契約。
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

# ── sys.path: tools subdirs (mirrors conftest.py) ──────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "scripts", "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

import _lib_prometheus as lp  # noqa: E402


def _mock_resp(body: bytes = b"OK") -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestSchemeValidation:
    """SSRF 防線：非 http(s) scheme 必須被拒、且不得發出任何請求。"""

    def test_rejects_non_http_scheme_without_network(self):
        with patch("urllib.request.urlopen") as mock_open:
            body, err = lp.probe_health("ftp://internal-host/-/healthy")
        assert body is None
        assert err == "Unsupported URL scheme: ftp"
        mock_open.assert_not_called()

    def test_rejects_file_scheme(self):
        with patch("urllib.request.urlopen") as mock_open:
            body, err = lp.probe_health("file:///etc/passwd")
        assert body is None
        assert "Unsupported URL scheme" in err
        mock_open.assert_not_called()


class TestTwoStateReturn:
    """雙態回傳：成功 (body, None)／失敗 (None, err_str)。"""

    def test_success_returns_body(self):
        with patch("urllib.request.urlopen", return_value=_mock_resp(
                b"Prometheus Server is Healthy.")):
            body, err = lp.probe_health("http://prom:9090/-/healthy")
        assert err is None
        assert body == "Prometheus Server is Healthy."

    def test_success_empty_body_is_not_error(self):
        with patch("urllib.request.urlopen", return_value=_mock_resp(b"")):
            body, err = lp.probe_health("http://prom:9090/-/healthy")
        assert err is None
        assert body == ""

    def test_failure_returns_str_of_exception(self):
        """err 字串 = str(exc)，與手抄站的 detail 字串逐字相容。"""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            body, err = lp.probe_health("http://prom:9090/-/healthy")
        assert body is None
        assert err == "refused"

    def test_timeout_kwarg_forwarded(self):
        with patch("urllib.request.urlopen", return_value=_mock_resp()) as m:
            lp.probe_health("http://prom:9090/-/healthy", timeout=3)
        assert m.call_args.kwargs["timeout"] == 3

    def test_non_utf8_body_does_not_fail(self):
        """errors='replace'：2xx 回應永不因 decode 失敗變成探活失敗。"""
        with patch("urllib.request.urlopen", return_value=_mock_resp(b"\xff\xfe")):
            body, err = lp.probe_health("http://prom:9090/-/healthy")
        assert err is None
        assert body is not None
