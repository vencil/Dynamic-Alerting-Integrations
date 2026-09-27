"""diagnose：依 db_type 決定 Pod／exporter 檢查，以及結束碼（issue 1513）。

本輪決策 N（owner 裁決 N2 原案）：Pod 檢查固定查 `app=mariadb`、exporter 檢查
固定查 `mysql_up`，所以非 MariaDB 租戶一律得到 `Pod not found`，batch-diagnose
對它們全報 error。改成：租戶的 db_type 是 mariadb 才跑這兩項；不是 mariadb 或
沒宣告時，兩項標 skipped 並附原因，不算 error。

db_type 從 Prometheus 的 `tenant_expected_exporter{tenant, db_type}` 讀：
batch-diagnose 呼叫 check() 時不帶 config_dir，讀 conf.d 在那條路徑上拿不到；
exporter 對宣告了 `_metadata.db_type` 的租戶發這條 series（#869）。

結束碼先前一律 0（`status: error` 也是），沒有 kubectl 時是 traceback。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_OPS = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "ops"
sys.path.insert(0, str(_OPS))
import diagnose  # noqa: E402


def _router(db_type="mariadb", up="1", fail=()):
    """依查詢的指標名回應；fail 內的指標名回 transport 錯誤。"""
    def fake(_url, query):
        name = query.split("{", 1)[0]
        if name in fail:
            return None, "connection refused"
        if name == "tenant_expected_exporter":
            if db_type is None:
                return [], None
            return [{"metric": {"tenant": "t1", "db_type": db_type},
                     "value": [0, "1"]}], None
        if name == "mysql_up":
            return [{"value": [0, up]}], None
        return [], None
    return fake


def _check(**kw):
    import io
    buf = io.StringIO()
    result = diagnose.check("t1", "http://prom:9090", out=buf)
    assert json.loads(buf.getvalue()) == result
    return result


@pytest.mark.parametrize("db_type,reason_word", [
    ("postgresql", "postgresql"),
    (None, "not declared"),
])
def test_non_mariadb_tenant_skips_pod_and_exporter(db_type, reason_word):
    with mock.patch.object(diagnose, "query_prometheus", side_effect=_router(db_type)), \
            mock.patch.object(diagnose, "run_cmd") as run_cmd:
        result = _check()
    assert result["status"] == "healthy", result
    assert [s["check"] for s in result["skipped"]] == ["pod", "exporter"]
    assert all(reason_word in s["reason"] for s in result["skipped"])
    run_cmd.assert_not_called()


def test_mariadb_tenant_still_runs_both_checks():
    with mock.patch.object(diagnose, "query_prometheus",
                           side_effect=_router("mariadb", up="0")), \
            mock.patch.object(diagnose, "run_cmd", side_effect=["Running", None]):
        result = _check()
    assert result["status"] == "error"
    assert any("mysql_up" in i for i in result["issues"])
    assert "skipped" not in result


def test_db_type_query_failure_is_reported_not_skipped_silently():
    with mock.patch.object(diagnose, "query_prometheus",
                           side_effect=_router(fail=("tenant_expected_exporter",))), \
            mock.patch.object(diagnose, "run_cmd"):
        result = _check()
    assert result["status"] == "error"
    assert any("Prometheus query failed" in i for i in result["issues"])


@pytest.mark.parametrize("result,rc", [
    ({"status": "healthy", "tenant": "t1"}, 0),
    ({"status": "error", "tenant": "t1", "issues": ["Pod not found"]}, 1),
    ({"status": "error", "tenant": "t1",
      "issues": ["Pod not found", "Prometheus query failed (http://x)"]}, 2),
])
def test_exit_code_follows_the_result(result, rc):
    assert diagnose.exit_code(result) == rc


def test_missing_kubectl_is_a_caller_error_not_a_traceback(tmp_path):
    """真的跑一次：PATH 上沒有 kubectl，Prometheus 回 db_type=mariadb。"""
    fake_prom = tmp_path / "prom.py"
    fake_prom.write_text(
        "import http.server, json, sys\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'status': 'success', 'data': {'resultType': 'vector',"
        " 'result': [{'metric': {'db_type': 'mariadb'}, 'value': [0, '1']}]}}).encode()\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json')\n"
        "        self.end_headers(); self.wfile.write(body)\n"
        "    def log_message(self, *a): pass\n"
        "s = http.server.HTTPServer(('127.0.0.1', 0), H)\n"
        "print(s.server_address[1], flush=True)\n"
        "s.serve_forever()\n",
        encoding="utf-8")
    server = subprocess.Popen([sys.executable, str(fake_prom)], stdout=subprocess.PIPE,
                              text=True, encoding="utf-8")
    try:
        port = server.stdout.readline().strip()
        env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_URL"}
        env["PATH"] = str(tmp_path)  # 沒有 kubectl
        proc = subprocess.run(
            [sys.executable, str(_OPS / "diagnose.py"), "t1",
             "--prometheus", f"http://127.0.0.1:{port}"],
            capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    finally:
        server.kill()
        server.wait(timeout=10)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "kubectl" in proc.stderr
    assert "Traceback" not in proc.stderr
