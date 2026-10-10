---
title: "ADR-001: 嚴重度 Dedup 採用 Inhibit 規則"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-001
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-03-13
updated_at: 2026-10-10
---
# ADR-001: 嚴重度 Dedup 採用 Inhibit 規則

> **Language / 語言：** **中文 (Current)** | [English](./001-severity-dedup-via-inhibit.en.md)

**決策摘要**：同一件事同時觸發 warning 與 critical 告警時，只通知 critical。這件事交給 Alertmanager 的抑制規則（`inhibit_rules`）在通知層處理；Prometheus 的告警規則不做去重，兩個等級照常觸發，也都留在 TSDB。

## 狀態

✅ **Accepted**（v1.0.0）

## 名詞

- **嚴重度去重（severity dedup）**：同一件事同時觸發多個嚴重度的告警時，只送出最高等級的通知。
- **抑制規則（inhibit rule）**：Alertmanager 的設定。符合「來源」條件的告警正在觸發時，符合「目標」條件的告警不送通知；`equal` 列出的標籤在兩邊必須同值，規則才成立。被抑制的告警仍然存在，只是不通知。
- **TSDB**：Prometheus 的時間序列資料庫。告警觸發時 Prometheus 會寫入 `ALERTS` 時間序列，事後可以查。
- **Rule Pack**：平台隨附的一組 Prometheus 規則檔（recording rule 與告警規則），每個檔案對應一種資料庫或用途，例如 `rule-pack-mariadb.yaml`。
- **`metric_group`**：告警規則上的標籤，用來把同一件事的 warning 與 critical 配成一對（兩者的告警名稱不同）。

## 背景

平台的閾值分 warning 與 critical 兩級。例如 CPU 使用率同時超過 70%（warning）與 90%（critical）時，值班的人只需要收到 critical 那一則。

去重可以放在兩個地方：

1. **PromQL 層**：在 warning 規則加上 `unless()` 或 `absent()`，critical 存在時 warning 就不觸發。
2. **Alertmanager 層**：兩個等級都照常觸發，由 `inhibit_rules` 擋下 warning 的通知。

## 決策

**採用 Alertmanager `inhibit_rules` 做嚴重度去重。** Prometheus 保留每個等級的完整告警紀錄，Alertmanager 只決定要不要通知。

抑制規則不用手寫：`generate_alertmanager_routes.py` 讀 conf.d/ 的租戶設定，替每個租戶產生一條。租戶設 `_severity_dedup: "disable"` 就不產生，兩個等級的通知都會收到。

### 範例

輸入：兩個租戶，`shop` 用預設值，`batch` 關掉去重。

```yaml
# conf.d/shop.yaml
tenants:
  shop:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/alerts"
```

```yaml
# conf.d/batch.yaml
tenants:
  batch:
    _severity_dedup: "disable"
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/batch"
```

```bash
python3 scripts/tools/ops/generate_alertmanager_routes.py --config-dir conf.d/ --dry-run
```

輸出（節錄）：只有 `shop` 得到抑制規則，`batch` 被略過。

```
  INFO: batch: severity_dedup disabled, skipping inhibit rule
...
inhibit_rules:
- source_matchers:
  - severity="critical"
  - metric_group=~".+"
  - tenant="shop"
  target_matchers:
  - severity="warning"
  - metric_group=~".+"
  - tenant="shop"
  equal:
  - metric_group
```

讀法：`shop` 有 critical 告警正在觸發時，同一個 `metric_group` 的 warning 告警不送通知。告警規則這一側要讓成對的告警帶相同的 `metric_group`，例如 Rule Pack 裡的 `MariaDBHighConnections`（warning）與 `MariaDBHighConnectionsCritical`（critical）都帶 `metric_group: "connections"`。

## 後果與已知限制

**得到的**

- TSDB 保留每個等級的告警紀錄，事後查得到 warning 觸發過幾次，不受 critical 是否同時觸發影響。
- 告警規則不必各自寫去重邏輯，去重集中在 Alertmanager 一處管理。
- 改抑制規則只需要 Alertmanager 重新載入設定，不必重啟 Prometheus。
- Alertmanager UI 看得到哪些告警被抑制，排查時能看到原本的狀態。

**要承擔的**

- Alertmanager 的設定變多，而且要和告警規則的標籤對齊：成對的告警缺 `metric_group`，或兩邊的值不同，抑制就不成立，會收到兩則通知。CI 會檢查同名成對的告警（`X` 與 `XCritical`）帶相同的 `metric_group`（`check_metric_group_pairs.py`）；名稱不成對的告警不在這個檢查的範圍內。
- Kubernetes Rule Pack 目前有 4 對告警（`PodContainerHighCPU`、`PodContainerHighMemory`、`PodContainerCPUThrottled`、`ContainerOOMKilled` 與各自的 `…Critical`）兩邊都沒有 `metric_group`，去重對它們不生效，會收到兩則通知。上面的 CI 檢查把這 4 對列為已知例外，只擋新增的違規（[#1199](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1199)）。
- 沒有 `metric_group` 標籤的告警不參與去重：抑制規則的來源與目標都要求 `metric_group=~".+"`。
- 建議定期檢視 Alertmanager 的抑制狀態，確認符合預期。

## 考慮過的替代方案

### PromQL 層去重（不採用）

在每條 warning 規則加上「critical 存在就不觸發」的條件。好處是規則本身自足，但：

- 被濾掉的 warning 不會觸發，TSDB 裡沒有它的紀錄，事後查不到某段時間 warning 等級的完整狀態。
- 平台工程師只看得到過濾後的結果，看不到原本的多等級狀態，不好排查。
- 每條告警規則都要手動加這段條件，容易漏。

### 由接收通知的一方去重（不採用）

讓每個接收端自己過濾。好處是與 Alertmanager 解耦，但同樣的邏輯要在每個接收端各做一次，無法統一管理。

## 相關

- [ADR-003: Sentinel Alert 模式](./003-sentinel-alert-pattern.md) — 同樣以抑制規則實作靜默模式
- [Config-Driven 架構設計 §2.8](../design/config-driven.md#28-severity-dedup嚴重度去重) — 行為矩陣與租戶設定
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — 抑制規則的產生器
