---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: zh
---

# ADR-001: 嚴重度 Dedup 採用 Inhibit 規則

## 狀態

✅ **Accepted** (v1.0.0)

> **v2.1.0 現況：** 此機制持續運作中。Severity Dedup 透過 `generate_alertmanager_routes.py` 自動生成 inhibit_rules，涵蓋 `_critical` 多層嚴重度，已通過 3070+ tests 驗證。

## 背景

在多層級告警系統中，同一指標常同時觸發多個嚴重度級別的告警。例如，CPU 使用率同時超過「警告」(70%) 和「嚴重」(90%) 閾值時，系統應只發送嚴重等級告警，抑制次級的警告等級告警。

### 問題陳述

需要選擇一個方式進行嚴重度去重（Severity Dedup），有兩個主要候選方案：

1. **PromQL 層級**：在告警規則中使用 `absent()` 或 `unless()` 運算子，若嚴重告警存在則過濾警告告警
2. **Alertmanager 層級**：使用 Alertmanager `inhibit_rules` 抑制告警

## 決策

**採用 Alertmanager `inhibit_rules` 進行嚴重度 Dedup。**

TSDB (時間序列資料庫) 保留所有嚴重度級別的完整指標數據，Alertmanager 在通知層級進行智慧抑制。

## 基本原理

### 為何拒絕 PromQL 方案

PromQL 層級的 `unless()` 或 `absent()` 方法存在根本缺陷：

- **TSDB 資料遺失**：被過濾的時間序列不會進入 TSDB，導致歷史資料不完整
- **後續查詢受限**：Prometheus 無法回溯某個時間段內的完整警告級別指標
- **調試困難**：平台工程師無法檢視原始的多層級告警狀態，只能看到最終被過濾的結果
- **可維護性差**：每條告警規則都需要手工增加 `unless()` 邏輯，易於出錯

### inhibit_rules 的優勢

- **TSDB 完整性**：所有級別的指標都被記錄，支援精細化分析與回溯
- **中央管理**：Alertmanager 的 `inhibit_rules` 集中定義，易於修改和維護
- **通知層級控制**：保留彈性，可根據路由、接收者等維度調整抑制邏輯
- **可監測性**：Alertmanager UI 清楚顯示被抑制的告警，便於故障排查

## 後果

### 正面影響

✅ TSDB 永遠保留完整資料，支援任意維度的歷史查詢
✅ Alertmanager 配置可動態重新加載，無需重啟 Prometheus
✅ 告警規則簡潔，邏輯集中在一處管理

### 負面影響

⚠️ Alertmanager 配置複雜度略高
⚠️ 需在 Alertmanager 與 Prometheus 之間同步嚴重度標籤定義

### 運維考量

- 使用 `generate_alertmanager_routes.py` 自動生成 inhibit_rules，降低手工錯誤
- 在 CI 中驗證 inhibit 規則與告警規則的標籤一致性
- 定期審計 Alertmanager 的抑制狀態，確保符合預期

## 替代方案考量

### 方案 A：PromQL 層級去重 (已拒絕)
- 優點：規則層級自完備
- 缺點：TSDB 資料遺失、可維護性差

### 方案 B：客戶端層級去重 (已拒絕)
- 優點：與 Alertmanager 解耦
- 缺點：複雜度轉移到 N 個客戶端，難以統一管理

## 相關決策

- [ADR-003: Sentinel Alert 模式](./003-sentinel-alert-pattern.md) — 利用 inhibit 實現三態控制

## 參考資料

- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.8 — 嚴重度 Dedup 設計細節
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — inhibit_rules 自動生成

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐⭐ |
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄-a角色與工具速查) | ⭐⭐ |
