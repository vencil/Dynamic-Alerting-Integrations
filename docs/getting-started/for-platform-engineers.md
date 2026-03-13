---
title: "Platform Engineer 快速入門指南"
tags: [getting-started, platform-setup]
audience: [platform-engineer]
version: v1.13.0
lang: zh
---
# Platform Engineer 快速入門指南

> **v1.13.0** | 適用對象：Platform Engineers、SRE、基礎設施管理員
>
> 相關文件：[Architecture](../architecture-and-design.md) · [Benchmarks](../architecture-and-design.md) · [GitOps Deployment](../gitops-deployment.md) · [Rule Packs](../../rule-packs/README.md)

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
python3 scripts/tools/validate_config.py --config-dir conf.d/ --schema
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

### 設定 Webhook Domain Allowlist

限制 webhook receiver 的目標域名：

```bash
python3 scripts/tools/generate_alertmanager_routes.py \
  --config-dir conf.d/ \
  --policy "*.example.com" \
  --policy "hooks.slack.com" \
  --validate
```

空清單表示不限制；fnmatch 模式支援萬用字元。

## 驗證工具

### 一站式配置驗證

```bash
python3 scripts/tools/validate_config.py \
  --config-dir conf.d/ \
  --schema
```

檢查項目：
- YAML 語法正確性
- 參數 schema 符合
- Route 轉換成功
- Policy 檢查通過
- 版本一致性

### 配置差異比對

```bash
python3 scripts/tools/config_diff.py \
  --old-dir conf.d.baseline \
  --new-dir conf.d/ \
  --format json
```

輸出：新增 tenant、移除 tenant、變更的預設值、變更的 profile。用於 GitOps PR review。

### 版號一致性檢查

```bash
make version-check
python3 scripts/tools/bump_docs.py --check
```

確保 CLAUDE.md、README、CHANGELOG 的版號同步。

## 效能監控

### 執行 Benchmark

```bash
make benchmark ARGS="--under-load --scaling-curve --routing-bench --alertmanager-bench --reload-bench --json"
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

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Platform Engineer 快速入門指南"](getting-started/for-platform-engineers.md) | ⭐⭐⭐ |
| ["Domain Expert (DBA) 快速入門指南"](getting-started/for-domain-experts.md) | ⭐⭐ |
| ["Tenant 快速入門指南"](getting-started/for-tenants.md) | ⭐⭐ |
| ["Migration Guide — 遷移指南"](./migration-guide.md) | ⭐⭐ |
