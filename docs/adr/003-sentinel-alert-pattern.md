---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.3.0
lang: zh
---

# ADR-003: Sentinel Alert 模式

## 狀態

✅ **Accepted** (v1.0.0)

> **v2.1.0 現況：** Sentinel 模式已擴展為完整的三態運營框架（Normal / Silent / Maintenance），支援 `expires` 自動失效與 `_state_maintenance` 維度標籤。所有新增 flag metric 一律採用此模式。

## 背景

平台支援三態運營模式：

- **Normal**：正常告警模式，觸發相應的通知
- **Silent**：靜默模式，完全抑制告警
- **Maintenance**：維護模式，特定告警被抑制

需要設計一個機制動態切換租戶的告警狀態，且組合性強、易於調試。

### 候選方案對比

| 方案 | 實現方式 | 可組合性 | 可觀測性 | 複雜度 |
|:-----|:--------|:-----:|:-----:|:-----:|
| 直接 PromQL 抑制 | 每條規則包裝 `unless(tenant_silent)` | ❌ 低 | ❌ 低 | 高 |
| Sentinel Alert + Inhibit | exporter flag → 告警 → inhibit | ✅ 高 | ✅ 高 | 中 |
| Alertmanager 路由 | 路由層面禁止通知 | ⚠️ 中 | ⚠️ 中 | 中 |

## 決策

**採用 Sentinel Alert 模式：exporter 發出租戶狀態 flag 指標 → recording rule 產生 sentinel 告警 → inhibit_rules 抑制相關告警。**

```
exporter (tenant_silent_mode)
  → recording rule (SentinelSilentMode)
    → sentinel alert (SilentModeActive)
      → inhibit rules (suppress other alerts for silent tenant)
```

## 基本原理

### 為何選擇 Sentinel Pattern

**Composability（可組合性）**：三態模式的任意組合都由同一套 inhibit 規則處理，新增狀態不需修改現有規則。

**Observability（可觀測性）**：Sentinel 告警在 Alertmanager 中可見，平台工程師可以清楚看到系統當前的三態狀態，便於故障排查。

**Decoupling（解耦合）**：告警規則與狀態控制邏輯分離。告警規則專注於檢測異常，狀態控制邏輯獨立在 exporter 層級。

### 架構流程

1. **Exporter 層級**：threshold-exporter 讀取租戶配置，發出 `tenant_silent_mode` / `tenant_maintenance_state` 等 flag 指標
2. **Prometheus 層級**：Recording rules 聚合 flag 指標，產生 `SentinelSilentMode` / `SentinelMaintenanceState` 中間指標
3. **告警規則層級**：Sentinel recording rules 轉譯為虛擬告警 (由 Prometheus rules 產生)
4. **Alertmanager 層級**：inhibit_rules 配對 sentinel 告警與業務告警，進行抑制

### 為何不直接用 PromQL

**脆弱性**：每條業務告警規則都需手工包裝 `unless(tenant_silent_mode)`，容易遺漏新增的規則

**不可維護**：Rule Pack 變更時，需同時更新所有告警規則的 `unless()` 子句

**無可觀測性**：使用者看不到抑制的邏輯，只知道告警消失了

## 後果

### 正面影響

✅ 三態邏輯集中在 Sentinel + Inhibit，易於維護與擴展
✅ Alertmanager UI 清楚顯示 sentinel alerts，便於調試
✅ 新增狀態時無需修改現有的業務告警規則
✅ 支援複雜的條件組合 (e.g., "silent OR maintenance")

### 負面影響

⚠️ 引入額外的中間層 (sentinel alerts)，增加概念複雜度
⚠️ Prometheus Rules 配置量增加 (額外的 recording rules)
⚠️ 調試時需同時檢視 exporter 指標、recording rules、inhibit 規則

### 運維考量

- 定期驗證 sentinel rules 與實際的狀態切換是否同步
- Alertmanager 日誌應記錄 inhibit 動作，便於稽核
- 文件應明確說明各狀態的優先級 (如 Silent 優先於 Maintenance)

## 替代方案考量

### 方案 A：直接 PromQL 抑制 (已拒絕)
- 優點：概念簡單
- 缺點：不可組合、難以維護、無可觀測性

### 方案 B：Alertmanager 路由層級 (已考量)
- 優點：無需修改告警規則
- 缺點：只能禁止通知，無法控制告警生成；難以應對複雜的租戶級邏輯

## 相關決策

- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — inhibit_rules 的基礎設計
- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — sentinel rules 作為 rule pack 一部分

## 參考資料

- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.7 — 三態運營模式詳細設計
- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.8 — Dedup 與 Sentinel 交互機制
- [`../rule-packs/README.md`](../rule-packs/README.md) — Rule Packs 總覽（含 Sentinel Recording Rules）

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐⭐ |
| [004-federation-scenario-a-first](004-federation-scenario-a-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐ |
| ["專案 Context 圖：角色、工具與產品互動關係"](../context-diagram.md) | ⭐⭐ |
