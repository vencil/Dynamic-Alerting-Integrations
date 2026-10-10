---
title: "ADR-003: Sentinel Alert 模式"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-003
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-03-13
updated_at: 2026-10-10
---
# ADR-003: Sentinel Alert 模式

> **Language / 語言：** **中文 (Current)** | [English](./003-sentinel-alert-pattern.en.md)

**決策摘要**：租戶的運營狀態（例如「靜默中」）由 threshold-exporter 輸出成旗標指標；Prometheus 的告警規則把旗標轉成一則 sentinel 告警；Alertmanager 以這則 sentinel 告警為抑制規則的來源，擋下該租戶告警的通知。業務告警規則完全不必知道這些狀態。

## 狀態

✅ **Accepted**（v1.0.0）

## 名詞

- **Sentinel 告警（哨兵告警）**：本身不代表故障，只用來表達「某個租戶目前處於某個狀態」的告警。平台的 sentinel 告警一律帶 `severity: none` 與 `component: sentinel`。
- **抑制規則（inhibit rule）**：Alertmanager 的設定。符合「來源」條件的告警正在觸發時，符合「目標」條件的告警不送通知；`equal` 列出的標籤在兩邊必須同值。被抑制的告警仍然存在，只是不通知（見 [ADR-001](./001-severity-dedup-via-inhibit.md)）。
- **Rule Pack**：平台隨附的一組 Prometheus 規則檔（recording rule 與告警規則），每個檔案對應一種資料庫或用途，例如 `rule-pack-mariadb.yaml`。
- **旗標指標**：exporter 依租戶設定輸出、值為 1 的指標，例如 `user_silent_mode`。設定拿掉，指標就消失。

## 背景

平台的租戶有三種運營狀態：

- **Normal**：正常觸發告警、正常通知。
- **Silent（靜默）**：告警照常觸發，但不送通知。
- **Maintenance（維護）**：以 `_state_maintenance` 開啟維護模式時，帶維護條件的告警規則不觸發。

需要一個機制，讓租戶的狀態可以隨設定動態切換，而且容易組合、容易排查。

### 候選方案比較

| 方案 | 做法 | 可組合性 | 可觀測性 | 複雜度 |
|:-----|:-----|:-----:|:-----:|:-----:|
| 直接在 PromQL 抑制 | 每條規則包一層 `unless`（狀態旗標存在就不觸發） | ❌ 低 | ❌ 低 | 高 |
| Sentinel 告警 + 抑制規則 | exporter 旗標 → sentinel 告警 → 抑制規則 | ✅ 高 | ✅ 高 | 中 |
| Alertmanager 路由 | 在路由層不送通知 | ⚠️ 中 | ⚠️ 中 | 中 |

## 決策

**採用 Sentinel 告警模式：exporter 輸出租戶狀態旗標 → 告警規則產生 sentinel 告警 → 抑制規則擋下相關告警的通知。**

這個模式處理的是「告警照常觸發、只擋通知」的狀態，也就是 Silent。

1. **exporter**：threshold-exporter 讀租戶設定，輸出旗標指標（`user_silent_mode`）。
2. **Prometheus**：Rule Pack 裡的告警規則讀旗標，產生 sentinel 告警（`TenantSilentWarning`、`TenantSilentCritical`）。
3. **Alertmanager**：抑制規則以 sentinel 告警為來源、同租戶的業務告警為目標，擋下通知。

### 範例

輸入：租戶 `shop` 只想靜默 warning 等級的通知。

```yaml
tenants:
  shop:
    _silent_mode: "warning"
```

threshold-exporter 的 `/metrics` 輸出：

```
user_silent_mode{target_severity="warning",tenant="shop"} 1
```

Rule Pack（`rule-pack-operational.yaml`）把它轉成 sentinel 告警：

```yaml
- alert: TenantSilentWarning
  expr: user_silent_mode{target_severity="warning"} == 1
  labels:
    severity: none
    component: sentinel
    tenant: "{{ $labels.tenant }}"
```

Alertmanager 的抑制規則：

```yaml
- source_matchers: ['alertname="TenantSilentWarning"', 'tenant=~".+"']
  target_matchers: ['severity="warning"', 'tenant=~".+"', 'alert_source=""']
  equal: ['tenant']
```

`alert_source=""` 表示目標只限沒有 `alert_source` 標籤的告警。平台自我監控的告警帶 `alert_source="platform"`（心跳告警 Watchdog 除外，它的 severity 是 none，本來就不在目標內），所以租戶的靜默設定擋不到平台自己的告警。

結果：`shop` 的 warning 告警照常觸發、留在 TSDB（Prometheus 的時間序列資料庫），通知被擋下；critical 告警照常通知。把 `_silent_mode` 拿掉，旗標指標消失，sentinel 告警解除，通知恢復。

## 後果與已知限制

**得到的**

- 新增這類「只擋通知」的狀態時，業務告警規則不用改：只要新增旗標、sentinel 告警規則與抑制規則。
- 狀態控制與異常偵測分開：告警規則只負責偵測異常，狀態由 exporter 依設定輸出。
- sentinel 告警在 Alertmanager UI 上看得到，排查時能直接看到哪個租戶處於什麼狀態，而不是只看到告警消失。

**要承擔的**

- 多了 sentinel 告警這一層，概念上較複雜；Rule Pack 也多了 sentinel 告警規則。
- 排查時要同時看 exporter 的旗標指標、sentinel 告警規則與抑制規則。
- sentinel 告警帶租戶標籤，若沒有區隔會被路由給租戶或值班中心當成通知送出。因此每則 sentinel 告警都帶固定的 `component="sentinel"`，Alertmanager 在租戶路由之前以一條路由把它導到不送通知的接收端（sentinel-sinkhole）。這條約定有測試守住：新增的 sentinel 告警沒帶這個標籤，測試會失敗。

**不涵蓋的範圍**

- **Maintenance 不走這個模式。** 抑制規則只擋通知，被擋的告警仍會觸發並留在 TSDB；以 `_state_maintenance` 開啟的維護模式要讓規則本身不觸發，所以用 PromQL 的 `unless`（行為對照見 [config-driven §2.7](../design/config-driven.md#27-三態運營模式-operational-modes)）。Rule Pack 的告警規則以 `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)` 排除維護中的租戶，帶這個條件的規則在維護期間不觸發，TSDB 也沒有紀錄。不是每條告警規則都帶這個條件。
- **嚴重度去重的 sentinel（`TenantSeverityDedupEnabled`）只用來顯示狀態。** 去重本身是 [ADR-001](./001-severity-dedup-via-inhibit.md) 的 critical→warning 抑制規則，不以 sentinel 為來源。

**運維建議**

- 定期確認 sentinel 告警與實際的狀態切換一致。
- 文件要寫清楚各狀態同時設定時的結果。

## 考慮過的替代方案

### 直接在 PromQL 抑制（不採用）

在每條業務告警規則包上「狀態旗標存在就不觸發」的條件。概念簡單，但：

- **容易漏**：每條規則都要手動包，新增的規則很容易忘記。
- **難維護**：Rule Pack 改版時，所有規則的這段條件要一起改。
- **看不見**：使用者只知道告警消失了，看不到是哪個狀態擋掉的。

### 在 Alertmanager 路由層處理（考慮過，不採用）

不必修改告警規則，但租戶層級的複雜邏輯不好表達。

## 相關

- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — 抑制規則的基礎設計
- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — sentinel 告警規則屬於 Rule Pack 的一部分
- [Config-Driven 架構設計 §2.7 三態運營模式](../design/config-driven.md#27-三態運營模式-operational-modes) — 三種狀態的行為矩陣、`expires` 自動失效與設定語法
- [`rule-packs/README.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/README.md) — Rule Pack 總覽
