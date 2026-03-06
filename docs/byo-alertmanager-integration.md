# BYO Alertmanager 整合指南

> **版本**：v1.5.0
> **受眾**：Platform Engineers、SREs
> **前置文件**：[BYO Prometheus 整合指南](byo-prometheus-integration.md)

---

## 1. 概述

告警疲勞的三大根因：(1) 備份/維護期間無法靜音 → 假陽性風暴；(2) Warning 與 Critical 同時通知 → 重複告警；(3) 通知目的地寫死在中央配置 → 租戶無法自主管理 on-call。Dynamic Alerting 平台透過 Alertmanager 的三大機制逐一解決：

| 功能 | 機制 | 配置來源 |
|------|------|----------|
| **Silent Mode** | Sentinel alert → inhibit_rules 攔截通知 | Tenant YAML `_silent_mode` |
| **Severity Dedup** | Per-tenant inhibit_rules（`metric_group` 配對） | Tenant YAML `_severity_dedup` |
| **Alert Routing** | Per-tenant route + receiver（webhook/email/slack/teams/rocketchat/pagerduty） | Tenant YAML `_routing` |

所有 Alertmanager 配置 fragment 由 `generate_alertmanager_routes.py` 從 tenant YAML 自動產出。

---

## 2. 整合步驟

### Step 1: 啟用 Alertmanager Lifecycle API

在 Alertmanager deployment 加入 `--web.enable-lifecycle` flag：

```yaml
args:
  - "--config.file=/etc/alertmanager/alertmanager.yml"
  - "--storage.path=/alertmanager"
  - "--web.enable-lifecycle"
```

驗證：

```bash
kubectl port-forward svc/alertmanager 9093:9093 -n monitoring &
curl -sf http://localhost:9093/-/ready && echo "OK"
```

### Step 2: 確保 Prometheus 連接 Alertmanager

```yaml
# prometheus.yml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - "alertmanager.monitoring.svc.cluster.local:9093"
```

### Step 3: 設定 Tenant Routing Config

在 tenant YAML 中定義 `_routing` section（v1.3.0 結構化 receiver）：

```yaml
# conf.d/db-a.yaml
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_by: ["alertname", "severity"]
      group_wait: "30s"
      repeat_interval: "4h"
```

### Step 4: 產出 Alertmanager Fragment

```bash
# da-tools 容器方式
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools:1.4.0 \
  generate-routes --config-dir /data/conf.d -o /data/alertmanager-routes.yaml

# 本地 Python 方式
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ -o alertmanager-routes.yaml

# 驗證產出
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --validate

# 驗證 + webhook domain allowlist 檢查（v1.5.0）
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --validate --policy .github/custom-rule-policy.yaml
```

產出內容包含：
- `route.routes[]`: Per-tenant 路由（含 `tenant="<name>"` matcher + timing guardrails）
- `receivers[]`: Per-tenant receiver（webhook/email/slack/teams/rocketchat/pagerduty）
- `inhibit_rules[]`: Per-tenant severity dedup rules

### Step 5: 合併至 Alertmanager ConfigMap

將產出的 fragment 合併至 Alertmanager 主配置：

```bash
# 一站式自動合併 + apply + reload（v1.4.0 推薦）
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ --apply --yes

# 或手動合併後 apply
kubectl create configmap alertmanager-config \
  --from-file=alertmanager.yml=alertmanager-merged.yml \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### Step 6: 重載 Alertmanager

```bash
# HTTP reload（需 Step 1 的 --web.enable-lifecycle）
curl -X POST http://localhost:9093/-/reload

# 驗證 reload 成功
curl -sf http://localhost:9093/-/ready && echo "Alertmanager ready"
```

---

## 3. generate_alertmanager_routes.py 工具

### 功能

讀取 `conf.d/` 所有 tenant YAML，掃描 `_routing` 和 `_severity_dedup` 設定，產出合法的 Alertmanager YAML fragment。

### 模式

| Flag | 說明 |
|------|------|
| `--dry-run` | 輸出至 stdout，不寫入檔案 |
| `-o FILE` | 寫入指定檔案 |
| `--validate` | 驗證配置合法性（exit 0/1，適合 CI） |

### Timing Guardrails

平台強制的 timing 範圍，超限自動 clamp：

| 參數 | 最小值 | 最大值 | 預設值 |
|------|--------|--------|--------|
| `group_wait` | 5s | 5m | 30s |
| `group_interval` | 5s | 5m | 5m |
| `repeat_interval` | 1m | 72h | 4h |

---

## 4. 動態 Reload

### 機制

v1.3.0 透過 Alertmanager 原生的 `--web.enable-lifecycle` flag 實現 HTTP reload：

```bash
# 更新 ConfigMap 後
curl -X POST http://alertmanager:9093/-/reload
```

### 自動化選項

| 方案 | 說明 | 適用場景 |
|------|------|----------|
| **HTTP reload** | `curl -X POST /-/reload`（v1.3.0 預設） | 最小侵入，適合自管 Alertmanager |
| **ConfigMap Watcher Sidecar** | 類似 `prometheus-config-reloader` | 全自動，適合生產環境 |
| **CI Pipeline 整合** | GitOps: `generate-routes --validate` + apply + reload | 適合 GitOps 工作流 |
| **Alertmanager Operator** | `kube-prometheus-stack` 的 AlertmanagerConfig CRD | 適合已使用 Operator 的環境 |

### _lib.sh Helper

```bash
source scripts/_lib.sh
reload_alertmanager  # 預設 http://localhost:9093
reload_alertmanager "http://alertmanager.monitoring.svc.cluster.local:9093"
```

---

## 5. Receiver 類型

v1.4.0 支援六種 receiver 類型：

### Webhook

```yaml
_routing:
  receiver:
    type: "webhook"
    url: "https://webhook.example.com/alerts"
```

### Email

```yaml
_routing:
  receiver:
    type: "email"
    to: ["team@example.com", "oncall@example.com"]
    smarthost: "smtp.example.com:587"
    from: "alertmanager@example.com"
    require_tls: true
```

### Slack

```yaml
_routing:
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T.../B.../xxx"
    channel: "#alerts"
```

### Microsoft Teams

```yaml
_routing:
  receiver:
    type: "teams"
    webhook_url: "https://outlook.office.com/webhook/..."
```

### Rocket.Chat

```yaml
_routing:
  receiver:
    type: "rocketchat"
    url: "https://chat.example.com/hooks/xxx/yyy"
    channel: "#alerts"        # metadata（不傳入 AM config）
    username: "PrometheusBot" # metadata（不傳入 AM config）
```

### PagerDuty

```yaml
_routing:
  receiver:
    type: "pagerduty"
    service_key: "your-integration-key"
    severity: "critical"
    client: "Dynamic Alerting Platform"
```

### 共通選填欄位

所有 receiver 類型都支援 `send_resolved: true`（預設 false），控制 alert 解除時是否發送通知。

### 訊息模板（Go Template）

Slack、Teams、Email 的 `title` / `text` / `html` 欄位支援 Alertmanager 原生的 Go template 語法，可引用 alert 的 labels、annotations、status：

**Slack 客製化範例：**

```yaml
_routing:
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/..."
    channel: "#db-alerts"
    title: '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }}'
    text: >-
      *Tenant*: {{ .CommonLabels.tenant }}
      *Severity*: {{ .CommonLabels.severity }}
      {{ range .Alerts }}
        - {{ .Annotations.summary }}
      {{ end }}
```

**Email HTML 模板範例：**

```yaml
_routing:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp.example.com:587"
    html: |
      <h2>{{ .CommonLabels.alertname }}</h2>
      <p>Tenant: {{ .CommonLabels.tenant }} | Severity: {{ .CommonLabels.severity }}</p>
      <ul>
      {{ range .Alerts }}
        <li>{{ .Annotations.description }}</li>
      {{ end }}
      </ul>
```

**可用的 Go template 變數：**

| 變數 | 說明 |
|------|------|
| `.CommonLabels.alertname` | Alert 名稱 |
| `.CommonLabels.tenant` | Tenant 名稱 |
| `.CommonLabels.severity` | 嚴重度（warning / critical） |
| `.CommonAnnotations.summary` | Alert 摘要 |
| `.CommonAnnotations.description` | Alert 描述 |
| `.Status` | 狀態（firing / resolved） |
| `.Alerts` | Alert 列表（可 `{{ range }}` 迴圈） |
| `{{ .Alerts \| len }}` | Alert 數量 |

> 完整語法參考：[Alertmanager Notification Template Reference](https://prometheus.io/docs/alerting/latest/notifications/)

---

## 6. 驗證 Checklist

### 工具驗證

```bash
# 1. 產出 fragment（dry-run 預覽）
da-tools generate-routes --config-dir /data/conf.d --dry-run

# 2. 驗證配置合法性
da-tools generate-routes --config-dir /data/conf.d --validate

# 3. 檢查 Alertmanager 狀態
curl -sf http://localhost:9093/-/ready

# 4. 查看當前 alert 狀態
curl -sf http://localhost:9093/api/v2/alerts | python3 -m json.tool
```

### 功能驗證

- [ ] `generate-routes --validate` exit code 0
- [ ] Alertmanager 載入合併後的配置無錯誤
- [ ] `curl -X POST /-/reload` 回傳 200
- [ ] Silent Mode tenant 的 alert 不發送通知
- [ ] Severity Dedup enabled tenant 的 warning 在 critical 觸發時被抑制
- [ ] Custom routing tenant 的 alert 送達指定 receiver

---

## Appendix: Alertmanager Operator 路徑

如果使用 `kube-prometheus-stack`（Prometheus Operator），可透過 `AlertmanagerConfig` CRD 管理路由：

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: tenant-db-a
  namespace: monitoring
spec:
  route:
    matchers:
      - name: tenant
        value: db-a
    receiver: tenant-db-a
  receivers:
    - name: tenant-db-a
      webhookConfigs:
        - url: "https://webhook.db-a.example.com/alerts"
```

此路徑與 `generate_alertmanager_routes.py` 的 ConfigMap 方式互斥。選擇 Operator 路徑的用戶不需要使用 `generate-routes` 工具。
