#!/usr/bin/env python3
"""check_alert.py — Check Prometheus alert state for a specific tenant.

Usage:
  # 本地開發 (透過 port-forward)
  kubectl port-forward svc/prometheus 9090:9090 -n monitoring &
  python3 check_alert.py HighConnectionCount db-a

  # 叢集內執行 (K8s Job / Pod)
  python3 check_alert.py HighConnectionCount db-a \
    --prometheus http://prometheus.monitoring.svc.cluster.local:9090

  # 多叢集 (Thanos / VictoriaMetrics)
  python3 check_alert.py HighConnectionCount db-a \
    --prometheus http://thanos-query.monitoring.svc:9090

Returns JSON: {"alert", "tenant", "state": "firing"|"pending"|"inactive"}

需求:
  - Prometheus Query API 必須可從腳本執行位置存取
    * 叢集內: K8s Service (http://prometheus.monitoring.svc.cluster.local:9090)
    * 叢集外: port-forward 或 Ingress
    * 多叢集: Thanos Query / VictoriaMetrics 等統一查詢端點亦可
"""
import urllib.request
import json
import sys
import argparse


def check_alert(alert_name, tenant, prom_url):
    try:
        req = urllib.request.Request(f'{prom_url}/api/v1/alerts')  # nosec B310
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
    except Exception as e:
        print(json.dumps({"error": f"Cannot connect to Prometheus API ({prom_url}): {e}"}))
        sys.exit(1)

    alerts = data.get('data', {}).get('alerts', [])

    # 過濾出符合 Alert Name 且包含特定 Tenant 標籤的警報
    matched_alerts = []
    for a in alerts:
        labels = a.get('labels', {})
        # 檢查 alertname
        if labels.get('alertname') != alert_name:
            continue
        # 檢查 tenant (可能存在於 tenant 或 instance 標籤中)
        if labels.get('tenant') == tenant or labels.get('instance') == tenant or tenant in str(labels):
            matched_alerts.append(a)

    if not matched_alerts:
        print(json.dumps({"alert": alert_name, "tenant": tenant, "state": "inactive"}))
        return

    # 找出最嚴重的狀態 (firing > pending)
    states = [a.get('state') for a in matched_alerts]
    if 'firing' in states:
        final_state = 'firing'
    elif 'pending' in states:
        final_state = 'pending'
    else:
        final_state = 'unknown'

    print(json.dumps({
        "alert": alert_name,
        "tenant": tenant,
        "state": final_state,
        "details": [{"state": a.get('state'), "activeAt": a.get('activeAt')} for a in matched_alerts]
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Check Prometheus alert state for a specific tenant",
    )
    parser.add_argument("alert_name", help="Alert name (e.g. HighConnectionCount)")
    parser.add_argument("tenant", help="Tenant ID (e.g. db-a)")
    parser.add_argument("--prometheus", default="http://localhost:9090",
                        help="Prometheus Query API URL "
                             "(預設: http://localhost:9090; "
                             "叢集內建議用 http://prometheus.monitoring.svc.cluster.local:9090)")
    args = parser.parse_args()
    check_alert(args.alert_name, args.tenant, args.prometheus)
