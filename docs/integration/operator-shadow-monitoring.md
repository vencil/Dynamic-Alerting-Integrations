---
title: "Operator Shadow Monitoring 策略"
tags: [operator, shadow-monitoring, migration]
audience: [platform-engineer]
version: v2.9.0
lang: zh
---
# Operator Shadow Monitoring 策略

> **Language / 語言：** **中文 (Current)** | [English](./operator-shadow-monitoring.en.md)

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **前置閱讀**：[Operator Alertmanager 整合](operator-alertmanager-integration.md)

---

## Overview

Shadow Monitoring（影子監控）是在 Operator 環境中變更告警路由時的雙軌觀察策略。它允許你在不影響生產告警的前提下，確認某個租戶的告警確實流經新的路由設定。

Operator 路徑上的路由是 `generate-routes` 渲染、經 Alertmanager CR 的 `spec.configSecret` 載入的一份 `alertmanager.yaml`（見 [Operator Alertmanager 整合](operator-alertmanager-integration.md)），所以影子路由寫在**你的來源（base）**裡，而不是另一份 AlertmanagerConfig CR。

---

## 策略：在來源裡加一條 `continue: true` 的影子路由

```yaml
# alertmanager.base.yaml（你的來源，不是 generate-routes 的輸出）
route:
  receiver: default
  routes:
    - receiver: db-a-shadow-webhook
      matchers:
        - tenant="db-a"
      continue: true                # ★ 關鍵：匹配後繼續往下，租戶路由照常收到
receivers:
  - name: default
  - name: db-a-shadow-webhook
    webhook_configs:
      - url: "http://shadow-collector:8080/collect"
        send_resolved: true
```

> 合併後的順序是：平台 4 條路由 → base 自己的路由（這條影子路由在這裡）→ 租戶路由。`continue: true` 讓告警在影子路由匹配後繼續往下走到租戶路由，同時送往生產 receiver 與影子 receiver。

⚠️ 不要用 AlertmanagerConfig CR 做影子路由：Operator 會替 CR 的路由加上 `namespace="<CR namespace>"` matcher，影子路由只看得到帶這個 label 的告警，比對結果不代表生產流量。

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
          image: ghcr.io/vencil/da-tools:v2.9.0
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

### Step 2: 把影子路由合併進生產設定

```bash
da-tools generate-routes --config-dir conf.d --output-alertmanager \
  --base-config alertmanager.base.yaml -o alertmanager.yaml
kubectl create secret generic da-alertmanager-config -n monitoring \
  --from-file=alertmanager.yaml --dry-run=client -o yaml | kubectl apply -f -
```

### Step 3: 比對生產 vs 影子

```bash
# 生產告警數量
curl -s 'http://localhost:9093/api/v2/alerts' | \
  jq '[.[] | select(.labels.tenant=="db-a")] | length'

# 影子收集器日誌
kubectl logs -n monitoring deploy/shadow-collector --tail=50
```

### Step 4: 確認無差異後移除影子路由

從 `alertmanager.base.yaml` 刪掉影子路由與它的 receiver，照 Step 2 重跑一次並更新 Secret。

---

## 注意事項

1. **影子路由看不到被平台路由攔下的告警**：平台 4 條路由排在 base 之前，被它們收下的告警（例如 `component="custom"` 隔離子樹裡的 custom alert）不會到達影子路由。
2. **效能影響**：影子路由會產生額外的 HTTP 請求。高流量環境建議限制影子觀察的 tenant 數量。
3. **清理**：觀察完畢後務必從來源刪除影子路由並重新產出，避免長期的額外 webhook 調用。

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | `configSecret` 路由流程 |
| [Operator GitOps 部署](operator-gitops-deployment.md) | CI/CD 整合 |
| [Shadow Monitoring SOP](../shadow-monitoring-sop.md) | 通用 Shadow Monitoring 流程 |
