---
title: "Operator Shadow Monitoring 策略"
tags: [operator, shadow-monitoring, migration]
audience: [platform-engineer]
version: v2.7.0
lang: zh
---
# Operator Shadow Monitoring 策略

> **Language / 語言：** **中文 (Current)** | [English](./operator-shadow-monitoring.en.md)

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **前置閱讀**：[Operator Alertmanager 整合](operator-alertmanager-integration.md)

---

## Overview

Shadow Monitoring（影子監控）是在 Operator 環境中進行路由遷移時的雙軌觀察策略。它允許你在不影響生產告警的前提下，驗證新的 AlertmanagerConfig 路由配置是否正確。

---

## 策略：雙 AlertmanagerConfig 並行

### 生產路由（現有）

```yaml
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: da-tenant-db-a
  namespace: monitoring
  labels:
    app.kubernetes.io/part-of: dynamic-alerting
    tenant: db-a
spec:
  route:
    receiver: db-a-pagerduty
    matchers:
      - name: tenant
        value: db-a
    groupBy: ["alertname", "instance"]
  receivers:
    - name: db-a-pagerduty
      pagerdutyConfigs:
        - routingKey:
            secret:
              name: da-db-a-pagerduty
              key: routing-key
```

### 影子路由（觀察用）

```yaml
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: da-shadow-db-a
  namespace: monitoring
  labels:
    app.kubernetes.io/part-of: dynamic-alerting
    tenant: db-a
    shadow: "true"
spec:
  route:
    receiver: db-a-shadow-webhook
    matchers:
      - name: tenant
        value: db-a
    continue: true                  # ★ 關鍵：不中斷路由，繼續匹配下一條
  receivers:
    - name: db-a-shadow-webhook
      webhookConfigs:
        - url: "http://shadow-collector:8080/collect"
          sendResolved: true
```

> `continue: true` 讓 Alertmanager 在匹配後繼續嘗試下一條路由，確保告警同時發送至生產 receiver 和影子 receiver。

---

## 觀察流程

### Step 1: 部署影子 Collector

部署一個輕量 webhook receiver 收集影子告警：

```bash
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shadow-collector
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: shadow-collector
  template:
    metadata:
      labels:
        app: shadow-collector
    spec:
      containers:
        - name: collector
          image: ghcr.io/vencil/da-tools:v2.7.0
          command:
            - python3
            - -c
            - |
              import json, sys
              from http.server import HTTPServer, BaseHTTPRequestHandler
              from datetime import datetime, timezone

              class WebhookHandler(BaseHTTPRequestHandler):
                  def do_POST(self):
                      length = int(self.headers.get('Content-Length', 0))
                      body = self.rfile.read(length)
                      try:
                          alerts = json.loads(body)
                          ts = datetime.now(timezone.utc).isoformat()
                          for alert in alerts.get('alerts', [alerts] if 'labels' in alerts else []):
                              record = {
                                  'timestamp': ts,
                                  'alertname': alert.get('labels', {}).get('alertname', ''),
                                  'tenant': alert.get('labels', {}).get('tenant', ''),
                                  'severity': alert.get('labels', {}).get('severity', ''),
                                  'status': alert.get('status', ''),
                                  'summary': alert.get('annotations', {}).get('summary', ''),
                              }
                              print(json.dumps(record), flush=True)
                      except Exception as e:
                          print(json.dumps({'error': str(e), 'raw': body.decode('utf-8', errors='replace')}), flush=True)
                      self.send_response(200)
                      self.send_header('Content-Type', 'application/json')
                      self.end_headers()
                      self.wfile.write(b'{"status":"ok"}')
                  def log_message(self, format, *args):
                      pass  # suppress default access logs

              print(json.dumps({'event': 'shadow-collector-started', 'port': 8080}), flush=True)
              HTTPServer(('', 8080), WebhookHandler).serve_forever()
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              cpu: 50m
              memory: 64Mi
---
apiVersion: v1
kind: Service
metadata:
  name: shadow-collector
  namespace: monitoring
spec:
  selector:
    app: shadow-collector
  ports:
    - port: 8080
EOF
```

> **Structured Logging**：shadow-collector 以 JSON Lines 格式輸出每筆影子告警，包含 `timestamp`, `alertname`, `tenant`, `severity`, `status`, `summary` 欄位。使用 `kubectl logs` 配合 `jq` 即可進行即時分析：
>
> ```bash
> kubectl logs -n monitoring deploy/shadow-collector -f | jq '.'
> ```

### Step 2: 部署影子 AlertmanagerConfig

```bash
kubectl apply -f shadow-alertmanagerconfig-db-a.yaml
```

### Step 3: 比對生產 vs 影子

```bash
# 生產告警數量
curl -s 'http://localhost:9093/api/v1/alerts' | \
  jq '[.data[] | select(.labels.tenant=="db-a")] | length'

# 影子收集器日誌
kubectl logs -n monitoring deploy/shadow-collector --tail=50
```

### Step 4: 確認無差異後切換

```bash
# 刪除影子路由
kubectl delete alertmanagerconfig da-shadow-db-a -n monitoring

# 更新生產路由（若有變更）
kubectl apply -f alertmanagerconfig-db-a-new.yaml
```

---

## 注意事項

1. **影子路由必須排在生產路由之前**：Alertmanager 按順序匹配路由，`continue: true` 需在較早的路由上設定。在同一 namespace 內，AlertmanagerConfig 按 metadata.name 字母序排列。
2. **效能影響**：影子路由會產生額外的 HTTP 請求。高流量環境建議限制影子觀察的 tenant 數量。
3. **清理**：觀察完畢後務必刪除影子 AlertmanagerConfig，避免長期的額外 webhook 調用。

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | AlertmanagerConfig 完整設定 |
| [Operator GitOps 部署](operator-gitops-deployment.md) | CI/CD 整合 |
| [Shadow Monitoring SOP](../shadow-monitoring-sop.md) | 通用 Shadow Monitoring 流程 |
