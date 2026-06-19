---
title: "ADR-026: Node/Cluster 維護告警抑制 — 不需要子系統"
tags: [adr, alerting, maintenance, k8s]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
id: ADR-026
tracking_kind: adr
status: proposed
domain: k8s
created_at: 2026-06-18
updated_at: 2026-06-19
---

# ADR-026: Node/Cluster 維護告警抑制 — 不需要子系統

## 狀態

🟡 **Proposed**（2026-06-18；2026-06-19 依客戶現場數據改寫）。owner 核可後昇格 Accepted。

> 依語言政策，ADR 自 ADR-019 起不另製 `.en.md`。

## TL;DR

- **問題**：Kubernetes node 維修、多叢集 rolling upgrade 時，想靜音受影響租戶的告警，又不波及其他叢集。
- **結論**：**不需要**一個「維護抑制子系統」。乾淨 drain 對架構正確（HA）的租戶幾乎不產生告警；現場數據也證實客戶 HA 叢集下沒有需要抑制的殘量。
- **三條決策**：
  1. **HA exporter 為主**——讓殘量自然歸零，零新元件。
  2. 客戶真正的 drain 噪音是**另一個問題**（告警粒度錯置），改走 HA-aware 語意分級（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)），不是維護抑制。
  3. 唯一一套維護抑制機制**設計完成、但 defer**（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)），reopen-trigger = 第一個非 HA 單實例客戶。
- **副產品**：探索過程挖到一個與維護無關的真實 liveness 缺陷，已獨立修復（P0，[#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)）。

## 背景：先分清「手段」和「目的」

起點是「想在 node 維修時做 node-level 靜音」。但靜音是手段，真正的目的是——

> **讓「預期內」的擾動對人隱形，同時讓「預期外」的真實事故照樣可見。**

用這把尺一量，「把整個 cluster 一鍵靜音」就出局了：它蓋掉預期內擾動的同時，也蓋掉維護窗內**非預期**的真實事故。

## 一個零成本檢查：乾淨 drain 到底會 fire 什麼

不開叢集、零成本——直接讀 rule pack，逐條問「乾淨 `kubectl drain` 期間這條會不會 fire、能不能用既有機制抑制」：

| 告警類別 | 乾淨 drain 會 fire？ | 既有 `_state_maintenance` 能抑制？ |
|---|---|---|
| `NodeNotReady`（for:3m） | cordon ≠ NotReady → **不會** | ✅ |
| `ContainerCrashLoop` | graceful evict ≠ crash → **不會** | n/a |
| 閾值類（如連線數過高） | 看負載 | ✅ |
| 租戶自訂告警（含 absence） | 看宣告 | ✅ 編譯器自動注入 |
| **平台 `*ExporterAbsent`**（critical） | **單實例 exporter 的節點被 drain → 會** | ❌ **← 唯一殘量** |

**結論：殘量很窄，只剩一類**——平台 `*ExporterAbsent` × 單實例 exporter。HA exporter（≥2 副本）下這條恆不 fire，殘量趨近於零。

（另有一類平台自我監控告警會在升級時響，但受眾是平台 SRE、且多半是「升級期間就該看到」的，處置是寫進 runbook，不是抑制。）

## 現場數據把方向改了（2026-06-19）

我們本來要為上面那個「單實例 `*ExporterAbsent`」殘量設計一套抑制機制。**但跨到實作時拿到客戶現場數據，方向被推翻：**

- 客戶的 mariadb / mongodb 都是 **HA 叢集**。drain 期間真正的噪音來自**告警粒度錯置**——`<db>_up==0` 是**單實例**級信號卻被定成 **Critical**，正常 failover 切換一台 replica 就誤 page。**完全不是** `*ExporterAbsent`（HA 下它恆不 fire）。

兩個後果：

1. **坐實「不建」**：我們正要花力氣抑制的殘量，在客戶的 HA 拓撲上**根本不會 fire**。
2. **客戶面工作改向**：客戶真正的痛是另一個問題 → 改走 HA-aware 語意分級（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)：instance 級降 warning、叢集存活用 quorum 信號定 critical）。

> **這一課**：多輪對抗式 review 把實作層（規則該寫幾條、維護該走哪個平面）磨得很細，卻沒先用真實現場數據驗證「**這是不是客戶的痛**」——差點為一個窄、且不是客戶痛點的殘量建一套常駐機制。**先驗方向，再投入實作深度。**

## 決策：不建子系統

1. **HA exporter 為主（最 durable 的解）。** 單實例 exporter 是殘量的根因；推 HA exporter（≥2 副本 + anti-affinity）讓殘量歸零。這是 SRE 正解（不替單點故障掩蓋告警），平台可降門檻（預設 HA exporter sidecar chart）。

2. **客戶面 drain 噪音交給 HA-aware 語意分級（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)），不是維護抑制。** instance 級存活降 warning、可被維護 opt-out；叢集存活改用 quorum 信號定 critical、不可 opt-out（drain 期間只要叢集選不出 primary 就必須叫醒）。單實例（非 HA）的期待值用一行說明管理即可，不建正式的 Service Tier 系統。

3. **重用既有機制、零新常駐元件。** silence / inhibit（[ADR-003](003-sentinel-alert-pattern.md)）+ `_state_maintenance` opt-out + 既有的 `maintenance_scheduler`。不引入 controller / operator，延續 [ADR-008](008-operator-native-integration-path.md) 的取消決策。

## 探索的副產品：一個真的 liveness 缺陷（[#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)）

查證 `*ExporterAbsent` 行為時，意外挖到一個**與維護無關**的真實缺陷，值得跟維護抑制分開講：

- 現行 `absent(mysql_up{job="tenant-exporters"})` 是**全域**判斷——只要還有任一租戶的 exporter 活著，它就不 fire。於是**某一租戶的 exporter 整個消失、其他租戶還在 → 零告警**（連完美 HA 租戶「整體」掛掉也中）。核心 liveness 承諾被靜默擊穿。

這跟「維護抑制」是兩件事，必須分開：

| 層 | 內容 | 狀態 |
|---|---|---|
| **liveness 正確性** | per-tenant 缺席偵測（專屬 expected-set metric + anti-join，用 `up==1` 同時接住「pod 消失」與「在但死」兩種情形） | **在修，P0 → [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)**（已 promtool 驗證；不在本 ADR 的 defer 範圍） |
| **維護抑制** | 在 liveness 告警上再疊一層維護 opt-out | **deferred → 下方「架構冷宮」 / [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)** |

本 ADR 的「不建子系統」只針對**維護抑制層**。liveness 正確性是獨立的 bug 修復，照自身價值排程；設計細節在 [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)，不在此重複。

## 架構冷宮（Deferred）：維護抑制層

> **為何保留**：設計成熟、零成本存放；但其前提（單實例殘量會 fire）在當前 HA 客戶上不成立。**reopen-trigger = 第一個非 HA 單實例客戶**。完整設計骨架見 [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)。

defer 的是「在 per-tenant liveness 告警上再疊一層維護 opt-out」，要點：

- **雙軌 opt-out**：計畫性維護走 config（`_state_maintenance`，慢但本就提前規劃）；緊急延長走 Alertmanager Silence API（秒級、不依賴 exporter 存活——config 平面要過 GitOps + owner 批太慢）。業界（Grafana）也是這樣分宣告式 / 即時兩種用途。
- **新建 `da-tools maintenance extend` CLI**：現行 `maintenance_scheduler.extend_silence` 是 Silence 平面、只被 CronJob 內部呼叫、無 CLI 入口（這兩點是查證修正過的事實）。
- **盲區護欄**：抑制 liveness = 維護窗內對真實 down 盲，故設 Max-TTL 自動到期 + 事後對賬。

> **為何 defer（而非現在就做）**：不是因為場景罕見（維護窗靜音是標準需求），而是三點不對稱——
> 1. **零現用 demand**：當前 HA 客戶不 fire 這個殘量（見上「現場數據」段）。
> 2. **下檔風險不對稱**：這功能本質是「維護期間靜音一條 critical liveness」，配置一錯就能藏住真實 outage。
> 3. **之後補很便宜**：真出現單實例客戶時，opt-out 只是在 [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869) 規則尾巴加一條 `unless on(tenant)(user_state_filter{maintenance}==1)`（`_state_maintenance` 機制已存在）。
>
> 即「等待成本≈一行、現在做≈替零 demand 開一個能靜音 critical 的口子」→ 維持 defer。

## 不做什麼

- **把整個 cluster 一鍵靜音**：會蓋掉預期外事故，是維護窗的 anti-pattern。
- **跟著 cordon 自動抑制的資料面子系統**：殘量窄到不值，違 [ADR-008](008-operator-native-integration-path.md)「不建 controller」。
- **動態生成 per-tenant rule / 自建 operator**：[ADR-008](008-operator-native-integration-path.md) 已取消。
- **為單實例建正式 Service Tier 系統**：過度，一行說明即可。

## 還沒做、各自留 trigger

- **維護抑制層**（上面的冷宮）：trigger = 第一個非 HA 單實例客戶（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)）。
- **多叢集 cluster-label 抑制**（A 靜音、B/C/D 正常）：trigger = 實際多叢集部署、且 `cluster` 標籤有流到 Alertmanager（現為單叢集）。
- **跟著 taint/cordon 的宣告式抑制**：手動 flag 在大規模下會累人，但需先建 node↔租戶的拓撲橋。trigger = 手動 flag 的規模痛點實際發生。
- **全 CRD-native / operator**：維持 [ADR-008](008-operator-native-integration-path.md) deferred。trigger = 客戶 RFP 要 kubectl 原生介面。

## 關聯

- [ADR-003](003-sentinel-alert-pattern.md) — sentinel + inhibit
- [ADR-008](008-operator-native-integration-path.md) — operator 取消；本 ADR 延續「不建 controller」
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — 自訂告警編譯器；租戶自訂告警自動吃 opt-out 的依據
- [ADR-025](025-alerting-plane-self-liveness.md) — 告警平面自我存活
- [生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md) — 本 ADR 所屬的 {角色 × 生老病死 × gate} SSOT
- [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) — HA-aware 語意分級（客戶當前的 drain 噪音）
- [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869) — per-tenant liveness 缺陷修復（P0，獨立於維護）
- [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870) — 維護抑制層（架構冷宮，deferred）
