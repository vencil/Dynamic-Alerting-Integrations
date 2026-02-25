#!/usr/bin/env python3
"""diagnose.py — Quick health check for a tenant's MariaDB and monitoring stack.

Usage: diagnose.py <tenant>
Returns JSON: {"status": "healthy"|"error", "tenant", ...}
Requires port-forward to Prometheus (localhost:9090).
"""
import subprocess
import sys
import json
import argparse

def run_cmd(cmd):
    """Execute a command safely using list arguments (no shell=True)."""
    if isinstance(cmd, str):
        import shlex
        cmd = shlex.split(cmd)
    try:
        return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError:
        return None

def check(tenant):
    errors = []

    # 1. 檢查 Pod 狀態
    pod_status = run_cmd(["kubectl", "get", "pods", "-n", tenant, "-l", "app=mariadb",
                          "-o", "jsonpath={.items[0].status.phase}"])
    if not pod_status:
        errors.append("Pod not found")
    elif pod_status != "Running":
        errors.append(f"Pod status is {pod_status}")

    # 2. 檢查 Exporter (透過 Prometheus API 靜默檢查)
    # 使用 localhost:9090 (假設 port-forward 已開啟)
    try:
        # 檢查 mysql_up
        up_res = run_cmd(["curl", "-s",
                          f"http://localhost:9090/api/v1/query?query=mysql_up{{instance=\"{tenant}\"}}"])
        if up_res and '"value":[1' not in up_res and '"value":["1"' not in up_res:
             errors.append("Exporter reports DOWN (mysql_up!=1)")
        elif not up_res:
             errors.append("Prometheus query failed (is port-forward running?)")
    except:
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
    parser = argparse.ArgumentParser()
    parser.add_argument("tenant")
    args = parser.parse_args()
    check(args.tenant)
