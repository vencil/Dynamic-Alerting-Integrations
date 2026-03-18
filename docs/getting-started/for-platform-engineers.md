---
title: "Platform Engineer 快速入門指南"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v2.2.0
lang: zh
---
# Platform Engineer 快速入門指南

> **v2.1.0** | 適用對象：Platform Engineers、SRE、基礎設施管理員
>
> 相關文件：[Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../gitops-deployment.md) · [Rule Packs](../rule-packs/README.md)

## 你需要知道的三件事

**1. threshold-exporter 是核心。** 它讀取 YAML 設定、產生 Prometheus Metrics、支援 SHA-256 hot-reload。兩個副本以 HA 方式運行在 port 8080。

**2. Rule Pack 是自成一體的單位。** 15 個 Rule Pack 透過 Projected Volume 掛載到 Prometheus，每個涵蓋一個資料庫或服務類型（MariaDB、PostgreSQL、Redis 等）。用 `optional: true` 機制安全卸載不需要的 Rule Pack。

**3. 一切都由配置驅動。** `_defaults.yaml` 控制平台全局行為，tenant YAML 覆蓋預設值，`_profiles.yaml` 提供繼承鏈。沒有硬編碼，沒有秘密。

## 30 秒快速部署

最小可用平台配置：

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_cpu: "75"
  mysql_memory: "85"
  # 其他預設閾值...
```

### 部署 threshold-exporter ×2 HA

```bash
kubectl apply -f k8s/02-threshold-exporter/
# 驗證副本運行
kubectl get pod -n monitoring | grep threshold-exporter
```

### 掛載 Rule Pack

```bash
# Prometheus StatefulSet 使用 Projected Volume
# 確認 k8s/03-monitoring/prometheus-statefulset.yaml 的 volume 部分
kubectl get configmap -n monitoring | grep rule-pack
```

> 💡 **互動工具** — 不確定需要哪些 Rule Pack？用 [Rule Pack Selector](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-selector.jsx) 互動選取。想估算叢集資源需求？試試 [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx)。不確定該選哪種架構？[Architecture Quiz](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/architecture-quiz.jsx) 幫你做決定。想在瀏覽器中體驗完整的工作流？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示 scaffold → validate → deploy。

## 常見操作

### 管理全局預設值

```yaml
# conf.d/_defaults.yaml
defaults:
  mysql_connections: "80"
  mysql_connections_critical: "95"
  container_cpu: "70"
  container_memory: "80"
  # 維度維持空閾值（跳過）
  redis_memory: "disable"      # 禁用
  _routing_defaults:
    group_wait: "30s"
    group_interval: "5m"
    repeat_interval: "12h"
```

驗證預設值語法：

```bash
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/ --schema
```

### 管理 Rule Pack

檢視已掛載的 Rule Pack：

```bash
kubectl get configmap -n monitoring | grep rule-pack
# 可能輸出：rule-pack-mariadb, rule-pack-postgresql, rule-pack-redis...
```

移除不需要的 Rule Pack（編輯 Prometheus StatefulSet）：

```bash
kubectl edit statefulset prometheus -n monitoring
# 在 volumes.projected.sources 中移除對應的 configMapRef
# 或設定 Projected Volume 的 optional: true 實現安全卸載
```

### 設定平台強制路由 (_routing_enforced)

啟用雙軌通知（NOC + Tenant）：

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_enforced:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/B/xxx"
      channel: "#noc-alerts"
    group_wait: "10s"
    repeat_interval: "2h"
```

NOC 收到的通知使用 `platform_summary` annotation，內容聚焦容量規劃和升級決策。Tenant 仍收到各自的 `summary`，不受影響。

### 設定路由預設值 (_routing_defaults)

```yaml
# conf.d/_defaults.yaml
defaults:
  _routing_defaults:
    receiver:
      type: "slack"
      api_url: "https://hooks.slack.com/services/T/{{tenant}}-alerts"
      channel: "#{{tenant}}-team"
    group_wait: "30s"
    repeat_interval: "4h"
```

`{{tenant}}` 佔位符自動展開為各 tenant 的名稱。Tenant YAML 的 `_routing` 可覆蓋此預設。

### 配置 Tenant Profile

```yaml
# conf.d/_profiles.yaml
profiles:
  standard-db:
    mysql_connections: "80"
    mysql_cpu: "75"
    container_memory: "85"
  high-load-db:
    mysql_connections: "60"     # 更嚴格
    mysql_cpu: "60"
    container_memory: "80"
```

Tenant 可透過 `_profile` 繼承：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"
    mysql_connections: "70"     # 覆蓋 profile 的值
```

### 配置 Routing Profiles 與 Domain Policies（v2.1.0 ADR-007）

當多個 tenant 共用相同的路由配置時，建立 `_routing_profiles.yaml` 定義命名路由設定檔：

```yaml
# conf.d/_routing_profiles.yaml
routing_profiles:
  team-sre-apac:
    receiver:
      type: slack
      api_url: "https://hooks.slack.com/sre-apac"
    group_wait: 30s
    repeat_interval: 4h
  team-dba-global:
    receiver:
      type: pagerduty
      service_key: "dba-key-123"
    repeat_interval: 1h
```

Tenant 透過 `_routing_profile` 引用，四層合併順序為 `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`。

**Domain Policies** 在 `_domain_policy.yaml` 中定義業務域合規約束（如金融域禁止 Slack）：

```yaml
# conf.d/_domain_policy.yaml
domain_policies:
  finance:
    tenants: [db-finance, db-audit]
    constraints:
      forbidden_receiver_types: [slack, webhook]
      max_repeat_interval: 1h
```

驗證指令：`da-tools check-routing-profiles --config-dir conf.d/`。偵錯指令：`da-tools explain-route --config-dir conf.d/ --tenant db-finance`。JSON Schema 可在 VS Code 中啟用即時驗證（見 `docs/schemas/`）。

### 設定 Webhook Domain Allowlist

限制 webhook receiver 的目標域名：

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy "*.example.com" \
  --policy "hooks.slack.com" \
  --validate
```

fnmatch 模式支援萬用字元。⚠️ 空清單表示不限制 — **生產環境強烈建議設定白名單**，避免 tenant 將告警發送到未授權的外部端點。

## 驗證工具

### 一站式配置驗證

```bash
python3 scripts/tools/ops/validate_config.py \
  --config-dir conf.d/ \
  --schema
```

檢查項目：
- YAML 語法正確性
- 參數 schema 符合
- Route 轉換成功
- Policy 檢查通過
- 版本一致性

### 告警品質評估（v2.1.0）

```bash
# 掃描所有 tenant 的告警品質（Noise / Stale / Latency / Suppression）
da-tools alert-quality --prometheus http://localhost:9090 --config-dir conf.d/

# CI gate：低於 60 分 exit 1
da-tools alert-quality --prometheus http://localhost:9090 --ci --min-score 60
```

### Policy-as-Code 策略驗證（v2.1.0）

```bash
# 用 _defaults.yaml 中的 _policies DSL 評估所有 tenant
da-tools evaluate-policy --config-dir conf.d/

# CI gate：有 error 違規時 exit 1
da-tools evaluate-policy --config-dir conf.d/ --ci
```

### 基數趨勢預測（v2.1.0）

```bash
# 預測 per-tenant 基數成長趨勢、觸頂天數
da-tools cardinality-forecast --prometheus http://localhost:9090

# CI gate：有 critical 風險時 exit 1
da-tools cardinality-forecast --prometheus http://localhost:9090 --ci
```

### 配置差異比對

```bash
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

輸出：新增 tenant、移除 tenant、變更的預設值、變更的 profile。用於 GitOps PR review。

### 版號一致性檢查

```bash
make version-check
python3 scripts/tools/dx/bump_docs.py --check
```

確保 CLAUDE.md、README、CHANGELOG 的版號同步。

## 效能監控

### 執行 Benchmark

```bash
make benchmark ARGS="--under-load --routing-bench --alertmanager-bench --reload-bench --json"
```

輸出指標：
- Idle memory footprint
- 延展曲線（QPS vs memory/latency）
- Routing throughput
- Alertmanager 反應時間
- ConfigMap reload 延遲

結果保存為 JSON，供 CI 比較。

### Platform Rule Pack 自監控

Platform 本身提供 Rule Pack alert（如 exporter 離線、Alertmanager delay > 1m）：

```bash
kubectl get alerts -n monitoring | grep platform
```

## 生產環境安全加固

完整的安全合規矩陣（Container 加固、NetworkPolicy、SAST、機密管理）見 [governance-security.md](../governance-security.md)。以下僅列 Platform Engineer 需要操作的關鍵項目。

### Port-forward 安全

本地 `kubectl port-forward` 預設綁定 `127.0.0.1`（僅本機）。**切勿使用 `--address 0.0.0.0`**，這會將 Prometheus/Alertmanager/Grafana 暴露到所有網路介面。

### Grafana 密碼

`secret-grafana.yaml` 提供開發環境 placeholder。生產部署前覆寫：

```bash
kubectl create secret generic grafana-credentials \
  --from-literal=admin-user=admin \
  --from-literal=admin-password="$(openssl rand -base64 24)" \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### Secrets 管理

Alertmanager receiver 的敏感資訊（Slack token、webhook URL 等）必須存放在 K8s Secret，不可用 ConfigMap。基本做法是 `secretKeyRef`，進階做法整合 External Secrets Operator + HashiCorp Vault（自動輪換 + 審計日誌）。

### Webhook Domain Allowlist

`generate_alertmanager_routes.py --policy` 的空清單表示不限制。**生產環境強烈建議設定白名單**。

### TLS 加密

元件間通訊應啟用 TLS。推薦用 cert-manager 簽發憑證，threshold-exporter 支援 `--tls-cert-file` / `--tls-key-file` 參數，Prometheus 用 `scheme: https` + `tls_config` 抓取。

## 常見問題

**Q: 我要如何新增一個 Rule Pack？**
A: 新 Rule Pack 需在 `rule-packs/` 目錄新增 YAML 檔案，並在 Prometheus Projected Volume 配置中掛載對應的 ConfigMap。請參考 Rule Pack README 的模板。

**Q: 如何強制 NOC 接收所有通知？**
A: 在 `_defaults.yaml` 中設定 `_routing_enforced`。通知會發送給 NOC 的 channel 和各 tenant 的 receiver，獨立進行。

**Q: Webhook allowlist 為何拒絕我的 domain？**
A: 用 `--policy` 檢查你的 webhook URL 是否符合 fnmatch 模式。例如 `*.example.com` 不會匹配 `webhook.internal.example.com`（多層子域名）。

**Q: 如何驗證新 tenant 的配置不會造成 alert noise？**
A: 先用 `validate_config.py` 檢查語法和 schema，再用 `config_diff.py` 看 blast radius，最後在 shadow monitoring 環境中測試（參考 shadow-monitoring-sop.md）。

**Q: Rule Pack 的 optional: true 是什麼？**
A: Kubernetes Projected Volume 的特性。設定 `optional: true` 後，如果該 ConfigMap 不存在，Prometheus 仍可啟動（卷掛載為空）。用於安全卸載 Rule Pack。

**Q: 我需要自定義某個 Rule Pack 中的規則嗎？**
A: 不直接修改 Rule Pack。在 tenant YAML 中用 `_routing.overrides[]` 覆蓋單個規則的路由，或用 custom rule governance（lint_custom_rules.py）新增自訂規則。

> 💡 **互動工具** — 驗證配置可用 [Config Lint](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-lint.jsx)。比較配置變更用 [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx)。查看 Rule Pack 依賴用 [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx)。完整 [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) 追蹤上線進度。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。需要在企業內網部署？用 `da-portal` Docker image：`docker run -p 8080:80 ghcr.io/vencil/da-portal`（[部署說明](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/da-portal/README.md)）。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐⭐ |
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
