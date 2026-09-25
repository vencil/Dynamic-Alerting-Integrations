"""
tests/test_lib_helpers.py — pytest style unit tests for Python snippets used in scripts/_lib.sh
Tests the pure-Python logic embedded in shell functions:
  - url_encode (urllib.parse.quote via stdin)
  - prom_query_value response parsing
  - get_alert_status response parsing
  - get_exporter_metric regex extraction
"""

import json
import os
import re
import subprocess
import sys
import urllib.parse

import pytest


# ---------------------------------------------------------------------------
# Helper: run an inline Python snippet the same way _lib.sh does
# ---------------------------------------------------------------------------
def run_python_snippet(code: str, stdin_data: str = "") -> str:
    """Execute a Python3 snippet and return stripped stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


# ===================================================================
# 1. url_encode — urllib.parse.quote via stdin
# ===================================================================

_URL_ENCODE_SNIPPET = (
    "import sys, urllib.parse; "
    "print(urllib.parse.quote(sys.stdin.read().strip()))"
)

def _encode(text: str) -> str:
    """編碼文字使用 URL encode。"""
    return run_python_snippet(_URL_ENCODE_SNIPPET, text)

def test_simple_metric():
    """測試簡單的 metric 名稱。"""
    assert _encode("mysql_up") == "mysql_up"

def test_spaces():
    """測試空格編碼。"""
    assert _encode("a b") == "a%20b"

def test_curly_braces():
    """測試大括號編碼和往返。"""
    expr = 'mysql_up{job="foo"}'
    encoded = _encode(expr)
    assert "%7B" in encoded
    assert "%7D" in encoded
    # round-trip
    assert urllib.parse.unquote(encoded) == expr

def test_single_quote_safe():
    """確保單引號不會導致問題（A1 發現）。"""
    expr = "rate(m{l='v'}[5m])"
    encoded = _encode(expr)
    assert urllib.parse.unquote(encoded) == expr

def test_empty_string():
    """測試空字串。"""
    assert _encode("") == ""

def test_complex_promql():
    """測試複雜 PromQL 表達式。"""
    expr = 'sum(rate(http_requests_total{method="GET"}[5m])) by (status)'
    encoded = _encode(expr)
    assert urllib.parse.unquote(encoded) == expr


# ===================================================================
# 2. prom_query_value — JSON response parsing
# ===================================================================

def _parse_prom_value(json_data: str, default: str = "N/A") -> str:
    """解析 Prometheus JSON 回應。"""
    snippet = f"""
import sys, json
try:
    r = json.load(sys.stdin)['data']['result']
    print(r[0]['value'][1] if r else '{default}')
except:
    print('{default}')
"""
    return run_python_snippet(snippet, json_data)

def test_prom_normal_result():
    """測試正常 Prometheus 結果。"""
    data = {"data": {"result": [{"value": [1234567890, "42"]}]}}
    assert _parse_prom_value(json.dumps(data)) == "42"

def test_prom_empty_result():
    """測試空 Prometheus 結果。"""
    data = {"data": {"result": []}}
    assert _parse_prom_value(json.dumps(data)) == "N/A"

def test_prom_malformed_json():
    """測試格式錯誤的 JSON。"""
    assert _parse_prom_value("{bad json") == "N/A"

def test_prom_missing_data_key():
    """測試缺少 data 鍵。"""
    assert _parse_prom_value(json.dumps({"status": "success"})) == "N/A"

def test_prom_custom_default():
    """測試自訂預設值。"""
    data = {"data": {"result": []}}
    assert _parse_prom_value(json.dumps(data), default="0") == "0"

def test_prom_float_value():
    """測試浮點數值。"""
    data = {"data": {"result": [{"value": [0, "3.14159"]}]}}
    assert _parse_prom_value(json.dumps(data)) == "3.14159"


# ===================================================================
# 3. get_alert_status — alert state extraction
# ===================================================================

def _parse_alert_status(json_data: str, alertname: str, tenant: str) -> str:
    """解析告警狀態。"""
    snippet = f"""
import sys, json
try:
    data = json.load(sys.stdin)
    alerts = [a for a in data['data']['alerts']
              if a.get('labels',{{}}).get('alertname') == '{alertname}'
              and '{tenant}' in str(a)]
    if any(a['state'] == 'firing' for a in alerts):
        print('firing')
    elif any(a['state'] == 'pending' for a in alerts):
        print('pending')
    else:
        print('inactive')
except:
    print('unknown')
"""
    return run_python_snippet(snippet, json_data)

def _make_alerts(*alert_defs):
    """建立告警資料結構。"""
    alerts = []
    for name, tenant, state in alert_defs:
        alerts.append({
            "labels": {"alertname": name, "tenant": tenant},
            "state": state,
        })
    return json.dumps({"data": {"alerts": alerts}})

def test_alert_firing():
    """測試 firing 狀態。"""
    data = _make_alerts(("MariaDBDown", "db-a", "firing"))
    assert _parse_alert_status(data, "MariaDBDown", "db-a") == "firing"

def test_alert_pending():
    """測試 pending 狀態。"""
    data = _make_alerts(("MariaDBDown", "db-a", "pending"))
    assert _parse_alert_status(data, "MariaDBDown", "db-a") == "pending"

def test_alert_inactive_no_match():
    """測試無符合告警時的 inactive 狀態。"""
    data = _make_alerts(("OtherAlert", "db-b", "firing"))
    assert _parse_alert_status(data, "MariaDBDown", "db-a") == "inactive"

def test_alert_firing_takes_precedence():
    """測試 firing 狀態優先於 pending。"""
    data = _make_alerts(
        ("MariaDBDown", "db-a", "pending"),
        ("MariaDBDown", "db-a", "firing"),
    )
    assert _parse_alert_status(data, "MariaDBDown", "db-a") == "firing"

def test_alert_empty_alerts():
    """測試空告警列表。"""
    data = json.dumps({"data": {"alerts": []}})
    assert _parse_alert_status(data, "MariaDBDown", "db-a") == "inactive"

def test_alert_malformed_json():
    """測試格式錯誤的 JSON。"""
    assert _parse_alert_status("{bad", "X", "Y") == "unknown"


# ===================================================================
# 4. get_exporter_metric — regex value extraction
# ===================================================================

_EXPORTER_PATTERN = r'\d+\.?\d*$'

def _extract_exporter_metric(line: str) -> str:
    """提取 exporter metric 的數值。"""
    m = re.search(_EXPORTER_PATTERN, line)
    return m.group(0) if m else ""

def test_exporter_integer_value():
    """測試整數值。"""
    line = 'user_threshold{tenant="db-a",metric="connections",severity="warning"} 70'
    assert _extract_exporter_metric(line) == "70"

def test_exporter_float_value():
    """測試浮點數值。"""
    line = 'user_threshold{tenant="db-a",metric="cpu",severity="warning"} 70.5'
    assert _extract_exporter_metric(line) == "70.5"

def test_exporter_no_value():
    """測試無數值。"""
    line = 'user_threshold{tenant="db-a"}'
    assert _extract_exporter_metric(line) == ""

def test_exporter_zero_value():
    """測試零值。"""
    line = 'user_state_filter{tenant="db-a",filter="maintenance"} 0'
    assert _extract_exporter_metric(line) == "0"

def test_exporter_large_number():
    """測試大數字。"""
    line = 'go_memstats_alloc_bytes{instance="exporter:8080"} 1234567890'
    assert _extract_exporter_metric(line) == "1234567890"


# ===================================================================
# 5. Structural checks
# ===================================================================

@pytest.fixture(scope="module")
def lib_sh_path():
    """提供 _lib.sh 檔案路徑。"""
    repo_root = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    return os.path.join(repo_root, "scripts", "_lib.sh")

def test_lib_file_exists(lib_sh_path):
    """驗證 _lib.sh 檔案存在。"""
    assert os.path.isfile(lib_sh_path)

def test_lib_shebang(lib_sh_path):
    """驗證 shebang 行。"""
    with open(lib_sh_path, encoding="utf-8") as f:
        first_line = f.readline().strip()
    assert first_line == "#!/bin/bash"

def test_lib_exported_functions_present(lib_sh_path):
    """驗證所有文件化的函數存在於 _lib.sh 中。"""
    with open(lib_sh_path, encoding="utf-8") as f:
        content = f.read()
    expected_functions = [
        "log", "warn", "err", "info",
        "setup_port_forwards", "cleanup_port_forwards",
        "prom_query_value", "get_alert_status", "wait_for_alert",
        "get_exporter_metric", "wait_exporter",
        "require_services",
        "url_encode", "kill_port",
        "ensure_kubeconfig", "preflight_check",
    ]
    for fn in expected_functions:
        assert f"{fn}()" in content, f"Function {fn}() not found in _lib.sh"

def test_lib_url_encode_uses_stdin(lib_sh_path):
    """驗證 url_encode 通過 stdin 傳遞輸入（A1 修復）。"""
    with open(lib_sh_path, encoding="utf-8") as f:
        content = f.read()
    # Should use stdin pattern, not inline $1
    assert "sys.stdin.read()" in content
