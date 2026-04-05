---
title: "場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知"
tags: [scenario, routing, dual-perspective]
audience: [platform-engineer]
version: v2.3.0
lang: zh
---
# 場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知

> **v2.3.0** | 相關文件：[`architecture-and-design.md` §2.9](../architecture-and-design.md)、[`byo-alertmanager-integration.md`](../byo-alertmanager-integration.md)

## 問題

同一個 alert（例如 `MariaDBHighConnections`）對不同角色有不同含義：

| 角色 | 關心的問題 | 期望的通知內容 |
|------|-----------|--------------|
| **Platform / NOC** | 哪個 tenant 受影響？影響面多大？需要升級嗎？ | 簡明的容量/升級提示，含 tier 資訊 |
| **Tenant** | 我的服務怎麼了？我能做什麼？ | 具體數值 + 建議動作 |

如果只用一個 `summary`，不是對 Platform 太模糊，就是對 Tenant 太技術。

## 解決方案：Dual-Perspective Annotation

在 Rule Pack 層級為每個 alert 預埋兩組 annotation：

```yaml
annotations:
  # Tenant 視角（原有，保持不變）
  summary: "High connections on {{ $labels.tenant }}"
  description: "{{ $value }} threads connected (warning threshold exceeded)"
  # Platform / NOC 視角
  platform_summary: "[{{ $labels.tier }}] {{ $labels.tenant }}: connection threshold breached — review connection pool sizing"
```

Alertmanager 的通知模板可以根據 receiver 選擇要引用哪個 annotation。

### 為什麼不用 Alertmanager 全域模板？

Alertmanager 的 notification template 是 **per-receiver-type 全域的**，不是 per-route。也就是說，你無法讓「走 webhook_configs 的 NOC route」和「走 webhook_configs 的 tenant route」使用不同模板。

Dual-Perspective Annotation 把差異前推到 Prometheus rule 層，讓 receiver 透過引用不同 annotation 欄位來實現分流，完全不需要修改 Alertmanager template 架構。

## 實作步驟

### 步驟 1：確認 Rule Pack 已有 platform_summary

v1.13.0 的所有 threshold alert（帶 `group_left(runbook_url, owner, tier)` 的）已預設包含 `platform_summary`。可以用以下命令確認：

```bash
grep -c platform_summary rule-packs/*.yaml
```

### 步驟 2：配置 `_routing_enforced` 的 receiver 模板

在 `_defaults.yaml` 或全域配置中，設定 `_routing_enforced` 的 receiver 使用 `platform_summary`：

```yaml
# _defaults.yaml 或全域 routing 配置
_routing_enforced:
  enabled: true
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/B/x"
    channel: "#noc-alerts"
    # Platform 視角：引用 platform_summary
    title: '{{ .Status | toUpper }}: {{ .CommonAnnotations.platform_summary }}'
    text: >-
      *Alert*: {{ .CommonLabels.alertname }}
      *Severity*: {{ .CommonLabels.severity }}
      *Owner*: {{ .CommonAnnotations.owner }}
      {{ range .Alerts }}
        - {{ .Annotations.platform_summary }}
      {{ end }}
  match:
    - 'severity=~"warning|critical"'
```

### 步驟 3：Tenant receiver 繼續使用 summary

Tenant 的 `_routing` 配置不需要任何變更。預設的 `summary` / `description` 本身就是 tenant 導向的：

```yaml
# conf.d/db-a.yaml — Tenant 不需改變
tenants:
  db-a:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/services/T/B/y"
        channel: "#db-a-alerts"
        title: '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }}'
        text: >-
          {{ range .Alerts }}
            {{ .Annotations.summary }}
            {{ .Annotations.description }}
          {{ end }}
```

### 步驟 4：Per-Tenant Enforced Channel（進階）

若 Platform 想讓每個 tenant 的 NOC 通知走不同 channel，利用 `{{tenant}}` 佔位符：

```yaml
_routing_enforced:
  enabled: true
  receiver:
    type: "slack"
    api_url: "https://hooks.slack.com/services/T/B/x"
    channel: "#noc-{{tenant}}"
    title: '{{ .CommonAnnotations.platform_summary }}'
```

系統會自動為每個 tenant 展開成獨立的 `platform-enforced-<tenant>` receiver。

## 自訂 platform_summary

### 方法 A：透過 `_metadata` 機制

Tenant 可以透過 `_metadata` 覆寫自己的 annotation（包含 `platform_summary`），但這通常由 Platform 管理：

```yaml
tenants:
  db-a:
    _metadata:
      # 這些會透過 tenant_metadata_info + group_left 注入
      runbook_url: "https://wiki.example.com/db-a"
      owner: "dba-team"
      tier: "tier-1"
```

> 注意：`platform_summary` 不在 `_metadata` 可覆寫的欄位中（它是 Rule Pack 的 annotation，不是 label）。若需要完全自訂，可以 fork Rule Pack 或使用 custom rule 覆寫。

### 方法 B：自訂 Rule Pack

如果組織需要完全不同的語義，可以 fork Rule Pack 修改 `platform_summary`：

```yaml
# my-custom-mariadb-rules.yaml
- alert: MariaDBHighConnections
  # ... existing expr ...
  annotations:
    summary: "高連線數：{{ $labels.tenant }}"
    platform_summary: "NOC 注意：{{ $labels.tenant }} 連線即將飽和，tier={{ $labels.tier }}，請評估是否需要通知客戶"
```

## 架構圖

```
                         ┌──────────────┐
                         │  Prometheus  │
                         │  Rule Pack   │
                         │              │
                         │  annotations:│
                         │   summary    │ ← tenant 視角
                         │   platform_  │ ← NOC 視角
                         │   summary    │
                         └──────┬───────┘
                                │
                       alert fires
                                │
                        ┌───────▼────────┐
                        │  Alertmanager  │
                        └───────┬────────┘
                                │
                 ┌──────────────┼──────────────┐
                 │              │              │
        ┌────────▼───────┐    ...    ┌────────▼───────┐
        │ platform-       │          │ tenant-db-a    │
        │ enforced route  │          │ route          │
        │ continue: true  │          │                │
        └────────┬───────┘          └────────┬───────┘
                 │                           │
        receiver uses:              receiver uses:
        platform_summary            summary
                 │                           │
        ┌────────▼───────┐          ┌────────▼───────┐
        │  #noc-alerts   │          │  #db-a-alerts  │
        │  (Platform)    │          │  (Tenant)      │
        └────────────────┘          └────────────────┘
```

## 注意事項

1. **向後相容**：原有只讀 `summary` 的 receiver 不受影響。`platform_summary` 是純粹的新增欄位。
2. **Sentinel alert 不含 platform_summary**：Operational Rule Pack 的 sentinel alert（如 `TenantSilentWarning`）本身就是平台級，不需要雙視角。
3. **Infrastructure alert 不含 platform_summary**：`XxxDown`、`ExporterAbsent` 等基礎設施 alert 本身已是平台觀點。
4. **Fallback**：若 receiver 模板引用 `platform_summary` 但某個 alert 沒有此 annotation，Alertmanager 會輸出空字串。建議使用 `{{ or .Annotations.platform_summary .Annotations.summary }}` 做 fallback。

## 互動工具

> 💡 **互動工具** — 下列工具可直接在 [Interactive Tools Hub](https://vencil.github.io/Dynamic-Alerting-Integrations/) 中測試：
>
> - [Config Diff](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-diff.jsx) — 比對路由配置變更
> - [Alert Simulator](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/alert-simulator.jsx) — 模擬告警流向和通知路由
> - [Config Lint](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/config-lint.jsx) — 驗證路由配置的正確性

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["場景：同一 Alert、不同語義 — Platform/NOC vs Tenant 雙視角通知"](alert-routing-split.md) | ⭐⭐⭐ |
| ["進階場景與測試覆蓋"](advanced-scenarios.md) | ⭐⭐ |
| ["場景：多叢集聯邦架構 — 中央閾值 + 邊緣指標"](multi-cluster-federation.md) | ⭐⭐ |
| ["場景：Shadow Monitoring 全自動切換工作流"](shadow-monitoring-cutover.md) | ⭐⭐ |
| ["場景：租戶完整生命週期管理"](tenant-lifecycle.md) | ⭐⭐ |
