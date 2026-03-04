# BYO Alertmanager 整合指南（藍圖）

> **版本**：v1.2.0（藍圖框架，v1.3.0 補完）
> **受眾**：Platform Engineers、SREs
> **前置文件**：[BYO Prometheus 整合指南](byo-prometheus-integration.md)

---

## 1. 概述

Dynamic Alerting 平台透過 Alertmanager 實現三大通知行為控制：

| 功能 | 機制 | 配置來源 |
|------|------|----------|
| **Silent Mode** | Sentinel alert → inhibit_rules 攔截通知 | Tenant YAML `_silent_mode` |
| **Severity Dedup** | Per-tenant inhibit_rules（`metric_group` 配對） | Tenant YAML `_severity_dedup` |
| **Alert Routing** | Per-tenant route + receiver | Tenant YAML `_routing` |

所有 Alertmanager 配置 fragment 由 `generate_alertmanager_routes.py` 從 tenant YAML 自動產出。

---

## 2. 最小整合步驟（概要）

### Step 1: 確保 Alertmanager 已與 Prometheus 連接

```yaml
# prometheus.yml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - "alertmanager.monitoring.svc.cluster.local:9093"
```

### Step 2: 產出 Alertmanager Fragment

使用 `da-tools generate-routes` 從 tenant 配置目錄產出 route + receiver + inhibit_rules：

```bash
# da-tools 容器方式
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools:1.2.0 \
  generate-routes --config-dir /data/conf.d -o /data/alertmanager-routes.yaml

# 本地 Python 方式
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir config/conf.d/ -o alertmanager-routes.yaml
```

產出內容包含：
- `route.routes[]`: Per-tenant 路由（含 `tenant="<name>"` matcher + timing guardrails）
- `receivers[]`: Per-tenant webhook receiver（v1.2.0 僅支援 `webhook_configs`）
- `inhibit_rules[]`: Per-tenant severity dedup rules + silent mode rules

### Step 3: 合併至 Alertmanager ConfigMap

將產出的 fragment 合併至 Alertmanager 主配置：

```bash
# 手動合併後 apply
kubectl create configmap alertmanager-config \
  --from-file=alertmanager.yml=alertmanager-merged.yml \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### Step 4: 重載 Alertmanager

```bash
# 目前方式：rolling restart
kubectl rollout restart deployment/alertmanager -n monitoring

# v1.3.0 目標：HTTP reload（需 --web.enable-lifecycle）
# curl -X POST http://alertmanager:9093/-/reload
```

---

## 3. generate_alertmanager_routes.py 工具

### 功能

讀取 `conf.d/` 所有 tenant YAML，掃描 `_routing` 和 `_severity_dedup` 設定，產出合法的 Alertmanager YAML fragment。

### Timing Guardrails

平台強制的 timing 範圍，超限自動 clamp：

| 參數 | 最小值 | 最大值 | 預設值 |
|------|--------|--------|--------|
| `group_wait` | 5s | 5m | 30s |
| `group_interval` | 5s | 5m | 5m |
| `repeat_interval` | 1m | 72h | 4h |

### Dry-run 模式

```bash
da-tools generate-routes --config-dir /data/conf.d --dry-run
# 輸出至 stdout，不寫入檔案
```

---

## 4. 動態 Reload 藍圖（v1.3.0）

### 現況（v1.2.0）

- Alertmanager ConfigMap 變更後需 rolling restart 才能生效
- 與 Prometheus 的 `--web.enable-lifecycle` + `curl /-/reload` 體驗不一致

### 目標

讓 Alertmanager 配置變更達到與 Prometheus 相同的「改設定不重啟」體驗。

### 候選方案

| 方案 | 說明 | 適用場景 |
|------|------|----------|
| **A. Lifecycle API** | Alertmanager 加入 `--web.enable-lifecycle`，ConfigMap 更新後 `curl -X POST /-/reload` | 最小侵入，適合自管 Alertmanager |
| **B. ConfigMap Watcher Sidecar** | 類似 `prometheus-config-reloader`，偵測 ConfigMap 變更後自動 POST reload | 全自動，適合生產環境 |
| **C. CI Pipeline 整合** | `generate_alertmanager_routes.py` 整合至 GitOps pipeline，ConfigMap update + reload 一步完成 | 適合 GitOps 工作流 |
| **D. Alertmanager Operator** | 使用 `kube-prometheus-stack` 的 AlertmanagerConfig CRD | 適合已使用 Operator 的環境 |

### 推薦路徑

v1.3.0 先實作方案 A（最小侵入），同時提供方案 B 的 sidecar 參考配置。方案 C/D 作為文件指引。

---

## 5. Receiver 類型擴充方向（v1.3.0）

v1.2.0 僅支援 `webhook_configs`。v1.3.0 計畫擴充：

| Receiver 類型 | 優先級 | 說明 |
|---------------|--------|------|
| `webhook_configs` | ✅ 已支援 | Generic webhook（GoAlert、PagerDuty webhook 等） |
| `email_configs` | 高 | 直接整合 SMTP |
| `slack_configs` | 高 | Slack Incoming Webhook |
| `msteams_configs` | 中 | Microsoft Teams（v0.27.0+ 原生支援） |
| `opsgenie_configs` | 低 | OpsGenie API |

擴充策略：`generate_alertmanager_routes.py` 的 `_routing` section 新增 `receiver_type` 欄位，預設 `webhook`。

---

## 6. 驗證 Checklist（概要）

- [ ] `da-tools generate-routes --config-dir conf.d/ --dry-run` 產出合法 YAML
- [ ] Alertmanager 載入合併後的配置無錯誤
- [ ] Silent Mode tenant 的 alert 不發送通知
- [ ] Severity Dedup enabled tenant 的 warning 在 critical 觸發時被抑制
- [ ] Custom routing tenant 的 alert 送達指定 webhook

> 完整 step-by-step 驗證流程將於 v1.3.0 補齊。
