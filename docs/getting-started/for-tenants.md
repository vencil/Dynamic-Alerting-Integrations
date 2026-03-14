---
title: "Tenant 快速入門指南"
tags: [getting-started, tenant-onboard]
audience: [tenant]
version: v2.0.0-preview.3
lang: zh
---
# Tenant 快速入門指南

> **v2.0.0-preview** | 適用對象：租戶（Tenant）管理者、DBA、SRE
>
> 相關文件：[Migration Guide](../migration-guide.md) · [Architecture](../architecture-and-design.md) §2 · [Rule Packs](../rule-packs/README.md)

## 你需要知道的三件事

**1. 你的監控已經啟用了。** 平台預載 15 個 Rule Pack，涵蓋 MariaDB、PostgreSQL、Redis、MongoDB、Elasticsearch、Kafka 等。只要你的 exporter 在跑，alert rules 就已經生效。

**2. 你只需要管理一個 YAML 檔案。** 所有自訂都在 `conf.d/<tenant>.yaml`，包括閾值調整、通知路由、維護窗口。

**3. 預設值很合理，你不一定需要改。** 除非你的業務場景需要更嚴格或更寬鬆的閾值，否則 `_defaults.yaml` 的預設已足夠。

## 30 秒快速配置

最小可用 tenant 配置只需要兩行：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant: {}
```

這會讓你的 tenant 使用所有預設閾值，沒有自訂路由（alert 會發到 Alertmanager 的 default receiver）。

## 常見操作

### 調整閾值

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # 連線數警告閾值（預設 80）
    mysql_connections_critical: "95"  # 連線數 critical 閾值
    container_cpu: "60"           # 容器 CPU 警告閾值（預設 70）
```

三態設計：每個指標可以設定 **自訂值**、**省略**（用預設）、或 `"disable"`（停用）。

> 💡 **互動工具** — 想即時驗證你的 YAML？試試 [YAML Playground](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/playground.jsx)。不確定閾值怎麼設？用 [Threshold Calculator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/threshold-calculator.jsx) 從 p50/p90/p99 推算。

```yaml
tenants:
  my-tenant:
    mysql_connections: "70"       # 自訂
    # mysql_cpu 省略               → 用 _defaults.yaml 的預設值
    container_memory: "disable"   # 停用此 alert
```

### 設定 Alert 通知路由

```yaml
tenants:
  my-tenant:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/services/T/B/xxx"
        channel: "#my-team-alerts"
      group_wait: "30s"
      repeat_interval: "4h"
```

支援的 receiver 類型：`webhook`、`email`、`slack`、`teams`、`rocketchat`、`pagerduty`。

### 使用 Profile 繼承

如果多個 tenant 有類似配置，可以使用 Profile 來避免重複：

```yaml
# conf.d/my-tenant.yaml
tenants:
  my-tenant:
    _profile: "standard-db"      # 繼承 _profiles.yaml 中的 standard-db
    mysql_connections: "50"       # 這會覆蓋 profile 的值
```

繼承順序：`_defaults.yaml` → `_profiles.yaml` → tenant 自訂值。Tenant 的值永遠最優先。

### 進入維護模式

```yaml
tenants:
  my-tenant:
    _state_maintenance:
      enabled: true
      expires: "2026-03-15T06:00:00Z"   # 自動恢復
      reason: "Planned DB migration"
```

維護模式下，alert 不會觸發（PromQL 層抑制）。到期後自動恢復。

### 靜默特定嚴重度

```yaml
tenants:
  my-tenant:
    _silent_mode:
      target: "warning"                  # 只靜默 warning
      expires: "2026-03-13T12:00:00Z"
      reason: "Known noisy alert during migration"
```

靜默模式下，alert 仍然會觸發（TSDB 有記錄），但 Alertmanager 不會發通知。

### 注入 Runbook / Owner / Tier

```yaml
tenants:
  my-tenant:
    _metadata:
      runbook_url: "https://wiki.example.com/my-tenant"
      owner: "dba-team"
      tier: "tier-1"
```

這些 metadata 會自動注入到所有 alert 的 annotation 中，出現在通知裡。

## 你會收到的通知

Alert 通知中的 `summary` 和 `description` 是為你（Tenant）撰寫的，告訴你：

- **什麼東西出了問題**（e.g., "High connections on my-tenant"）
- **具體數值**（e.g., "150 threads connected"）
- **你可以做什麼**（在 description 或 runbook 中）

> 如果你的 Platform 團隊啟用了 `_routing_enforced`，他們會同時收到 platform 視角的摘要（`platform_summary`），內容偏向容量規劃和升級判斷。你不需要關心這部分 — 你的通知不受影響。

> 💡 **互動工具** — 想知道你會收到哪些告警？用 [Alert Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/alert-simulator.jsx) 模擬。選 Rule Pack 不確定？試試 [Rule Pack Selector](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/rule-pack-selector.jsx)。

## 自助驗證

### 驗證配置

```bash
# 一站式驗證（YAML 語法 + Schema + Route + Profile）
python3 scripts/tools/ops/validate_config.py --config-dir conf.d/
```

### 檢視繼承鏈

```bash
# 看你的 tenant 最終解析出哪些閾值
python3 scripts/tools/ops/diagnose.py my-tenant \
  --config-dir conf.d/ --show-inheritance
```

### 預覽配置變更影響

```bash
# 比較 before / after 的 blast radius
python3 scripts/tools/ops/config_diff.py \
  --old-dir conf.d.baseline --new-dir conf.d/
```

## 產生配置（互動式）

第一次接入？使用 scaffold 工具：

```bash
python3 scripts/tools/ops/scaffold_tenant.py
```

它會問你幾個問題（DB 類型、通知方式），然後自動產生完整的 YAML 檔案。

## 常見問題

**Q: 我修改了 YAML 後多久生效？**
A: threshold-exporter 每 15 秒檢查 ConfigMap 的 SHA-256 hash。偵測到變更後 hot-reload，不需重啟。

**Q: 我可以只用部分 Rule Pack 嗎？**
A: 不需要用的 Rule Pack 不會產生 alert（沒有對應的 exporter metric = 沒有數據 = 不觸發）。如果你想完全移除，Projected Volume 的 `optional: true` 機制允許安全卸載。

**Q: _profile 和直接設定有什麼差別？**
A: Profile 是填充（fill-in），只在 tenant 沒有設定該 key 時生效。你的直接設定永遠優先。

**Q: 我怎麼知道現在有哪些 metric key 可以設定？**
A: 查看 `_defaults.yaml` 和各 Rule Pack YAML 的頂部註解。也可以執行 `diagnose.py --show-inheritance` 看完整的可用 key。

> 💡 **第一次上線？** 用 [Onboarding Checklist](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/onboarding-checklist.jsx) 取得完整的步驟清單，或從 [互動式入門精靈](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../getting-started/wizard.jsx) 開始。想在瀏覽器中觀看完整的平台運作流程？[Platform Demo](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/platform-demo.jsx) 展示真實場景。所有工具見 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/)。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Tenant 快速入門指南"](for-tenants.md) | ⭐⭐⭐ |
| ["Migration Guide — 遷移指南"](../migration-guide.md) | ⭐⭐ |
| ["Domain Expert (DBA) 快速入門指南"](for-domain-experts.md) | ⭐⭐ |
| ["Platform Engineer 快速入門指南"](for-platform-engineers.md) | ⭐⭐ |
