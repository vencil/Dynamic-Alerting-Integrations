#!/usr/bin/env python3
"""diagnose.py — Quick health check for a tenant's MariaDB and monitoring stack.

Usage:
  # 本地開發 (透過 port-forward)
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &
  python3 diagnose.py db-a

  # 叢集內執行 (K8s Job / Pod)
  python3 diagnose.py db-a \
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # 多叢集 (Thanos / VictoriaMetrics)
  python3 diagnose.py db-a \
    --prometheus http://thanos-query.monitoring.svc:9090

Returns JSON: {"status": "healthy"|"error", "tenant", ...}

需求:
  - Prometheus Query API 必須可從腳本執行位置存取
    * 叢集內: K8s Service (http://prometheus.monitoring.svc.cluster.local:9090)
    * 叢集外: port-forward 或 Ingress
    * 多叢集: Thanos Query / VictoriaMetrics 等統一查詢端點亦可
"""
import subprocess
import sys
import json
import argparse
import urllib.request
import urllib.parse


def run_cmd(cmd):
    """Execute a command safely using list arguments only (no shell=True).

    Args:
        cmd: Command as a list of strings. String input is rejected
             to prevent potential command injection via shlex parsing.
    """
    if not isinstance(cmd, list):
        raise TypeError(f"run_cmd() requires list argument, got {type(cmd).__name__}")
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return None


def query_prometheus(prom_url, promql):
    """執行 Prometheus instant query，回傳原始 JSON 字串。"""
    url = f"{prom_url}/api/v1/query"
    params = urllib.parse.urlencode({"query": promql})
    full_url = f"{url}?{params}"
    try:
        req = urllib.request.Request(full_url)  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode()
    except Exception:
        return None


def check(tenant, prom_url):
    errors = []

    # 1. 檢查 Pod 狀態
    pod_status = run_cmd(["kubectl", "get", "pods", "-n", tenant, "-l", "app=mariadb",
                          "-o", "jsonpath={.items[0].status.phase}"])
    if not pod_status:
        errors.append("Pod not found")
    elif pod_status != "Running":
        errors.append(f"Pod status is {pod_status}")

    # 2. 檢查 Exporter (透過 Prometheus API)
    try:
        up_res = query_prometheus(prom_url, f'mysql_up{{instance="{tenant}"}}')
        if up_res and '"value":[1' not in up_res and '"value":["1"' not in up_res:
            errors.append("Exporter reports DOWN (mysql_up!=1)")
        elif not up_res:
            errors.append(f"Prometheus query failed ({prom_url})")
    except Exception:
        errors.append("Metrics check failed")

    # 3. 輸出結果 (Token Saving 核心：正常時只回傳極簡 JSON)
    if not errors:
        print(json.dumps({"status": "healthy", "tenant": tenant}))
    else:
        # 只有異常時，嘗試抓取最近的 error log
        logs = run_cmd(["kubectl", "logs", "-n", tenant, "deploy/mariadb", "-c", "mariadb", "--tail=20"])
        error_logs = [line for line in logs.split('\n') if 'ERROR' in line] if logs else []

        print(json.dumps({
            "status": "error",
            "tenant": tenant,
            "issues": errors,
            "recent_logs": error_logs[:3]  # 只回傳最後 3 行錯誤
        }, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Quick health check for a tenant's MariaDB and monitoring stack",
    )
    parser.add_argument("tenant", help="Tenant ID (e.g. db-a)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus Query API URL "
                             "(預設: http://localhost:9090; "
                             "叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)")
    args = parser.parse_args()
    check(args.tenant, args.prometheus)
