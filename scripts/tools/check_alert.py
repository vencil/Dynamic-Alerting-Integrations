#!/usr/bin/env python3
"""check_alert.py — Check Prometheus alert state for a specific tenant.

Usage: check_alert.py <alert_name> <tenant>
Returns JSON: {"alert", "tenant", "state": "firing"|"pending"|"inactive"}
Requires port-forward to Prometheus (localhost:9090).
"""
import urllib.request
import json
import sys

def check_alert(alert_name, tenant):
    try:
        req = urllib.request.Request('http://localhost:9090/api/v1/alerts')
        with urllib.request.urlopen(req) as response:  # nosec B310 — localhost-only Prometheus API
            data = json.loads(response.read().decode())
    except Exception as e:
        print(f'{{"error": "Cannot connect to Prometheus API: {e}. Is port-forward running?"}}')
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
    if len(sys.argv) != 3:
        print("Usage: check_alert.py <alert_name> <tenant>")
        sys.exit(1)
    check_alert(sys.argv[1], sys.argv[2])
