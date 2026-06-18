---
title: "ADR-026: Node/Cluster 維護告警抑制 — Liveness-Class Gap，不是子系統"
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

# ADR-026: Node/Cluster 維護告警抑制 — Liveness-Class Gap，不是子系統

## 狀態

🟡 **Proposed**（2026-06-18；**現場數據 amend 2026-06-19**）。owner 核可後昇格 Accepted。

> 依語言政策，ADR 自 ADR-019 起不另製 `.en.md`。
>
> **這份 ADR 走過：設計 → 實作前深審 → 現場數據反轉。** 淨結果：**「不建維護抑制子系統」的決策不動，且被現場數據坐實**；原本為 `*ExporterAbsent` 殘量設計的抑制機制（anti-join 2 條規則 / 雙軌維護 / extend CLI）**設計完成但 defer 進下方「架構冷宮」段**（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)，reopen-trigger = 第一個非 HA 單實例客戶）；客戶面的 drain 噪音改由 **HA-aware 語意分級**承接（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)）。

## TL;DR

- **問題**：Kubernetes node 維修 / 多叢集 rolling upgrade 時，想靜音受影響租戶的告警，又不波及其他叢集。
- **結論**：**不需要**一個「維護抑制子系統」。**Gate 1（零成本靜態實證）**顯示乾淨 drain 對架構正確（HA）的租戶幾乎不產生告警，唯一殘量是**單實例 exporter 的 `*ExporterAbsent`**；**2026-06-19 現場數據再坐實**——客戶 HA 叢集下這個殘量根本不 fire，且客戶真正的 drain 噪音是**另一個問題**（HA 告警粒度錯置）。
- **決策**：(1) **HA exporter 為主**讓殘量自然歸零；(2) 客戶面 drain 噪音 = **HA-aware 語意分級**（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)），不是本案的 liveness 抑制；(3) `*ExporterAbsent` 抑制機制**設計完成、defer 進架構冷宮**（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)），reopen-trigger = 第一個非 HA 單實例客戶。**零新常駐元件**。

## 背景：先把目的校正回來

起點是「想在 node 維修時做 node-level 靜音」。但真正的目的不是「靜音」本身，而是——

> **讓「預期內」的擾動對人隱形，同時讓「預期外」的擾動照樣可見。**

用這把尺一量，「blanket 把整個 cluster 靜音」就出局了：它在靜音預期內擾動的同時，也把維護窗內**非預期**的真實事故一起蓋掉，不通過目的。

## Gate 1：乾淨 drain 到底會 fire 什麼

不開叢集、零成本——直接讀 rule pack，逐條問「乾淨 `kubectl drain` 期間這條會不會 fire、能不能用既有機制抑制」：

| 告警類別 | 乾淨 drain 會 fire？ | 既有 `_state_maintenance` 抑制？ |
|---|---|---|
| `NodeNotReady`（for:3m） | cordon ≠ NotReady → **不會**（除非 reboot 階段 node down >3min） | ✅ 經 `node_owner` opt-out |
| `ContainerCrashLoop` | graceful evict ≠ crash → **不會** | n/a |
| 閾值類（如 `MariaDBHighConnections`） | 看負載 | ✅ 帶 `unless on(tenant) user_state_filter{maintenance}` |
| Custom recipe（含 `absence`） | 看宣告 | ✅ 編譯器無條件注入 opt-out |
| **平台 `*ExporterAbsent`**（critical, for:30s） | **單實例 exporter 的節點被 drain → 會** | ❌ **無 opt-out ← 唯一殘量** |

**判決：narrow PARTIAL。** 乾淨 drain 的唯一不可抑制殘量 = **平台 `*ExporterAbsent` 類 × 單實例 exporter**。HA exporter（≥2 副本）下 `absent()` 恆 false、殘量≈0。精確地說，殘量由 `absent()`（exporter pod 被 evict、`<up>` series 缺席）觸發，不是 `<db>Down`（`_up==0` 需 series 在場為 0）。

第二類殘量（平台自我監控 pack：`ThresholdExporterDown`/`TooFewReplicas`…）受眾是**平台 SRE**、且多半是「升級期間**該看到**」的（`TooFewReplicas` = HA 正在降級）→ 處置是**升級 runbook 預期它**，不是抑制。

## 現場數據驗證（2026-06-19）— 設計→實作邊界的閘門

Gate 1 把唯一殘量定位在 `*ExporterAbsent` × 單實例，我們據此開始設計抑制機制（anti-join 2 條規則 + 雙軌維護，見下方「架構冷宮」段）。**跨到實作時拿到客戶現場數據，方向被修正：**

- 客戶 mariadb/mongodb 都是 **HA cluster**。planned node drain 的噪音來自 **HA 告警粒度錯置**——`<db>_up==0` 是**單實例**級信號卻被定 **Critical**，正常 failover 切換一台 replica 就誤 page。**不是** `*ExporterAbsent`（HA → `absent()` 恆 false → 從不 fire）。

這同時**坐實**與**改向**兩件事：

1. **坐實 not-build**：我們正要建機制去抑制的那個殘量，在客戶的 HA 拓撲上**根本不 fire**。Gate 1 的「HA → 殘量≈0」從靜態推論變成現場事實。
2. **改向客戶面工作**：客戶真正的痛是**另一個問題**（HA mis-tiering），那套 `*ExporterAbsent` 抑制機制本來就解不到 → 客戶面改走 **HA-aware 語意分級**（[#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)：instance=warning/可 opt-out、cluster=quorum-aware critical/不可 opt-out）。

> **教訓（means→ends 再現）**：對抗式深審把實作層（1→2 條規則、config→雙軌）打磨得很細，卻沒先用 field data 校驗「**這是不是客戶的痛**」——差點為一個「窄、且不是客戶痛點」的殘量建一套常駐機制。**field-data validation 屬於 design→implementation 邊界的閘門，不是實作中途的選配。**

## 決策：不建子系統，三條 locked decision

1. **HA exporter 為主（最 durable 的解）。** 單實例 exporter 是殘量的根因，HA exporter（≥2 副本 + pod anti-affinity）讓 `absent()` 恆 false、殘量歸零。這是 SRE 正解（不替單點故障掩蓋告警），不是把問題推給租戶。平台可降門檻（預設 HA exporter sidecar chart）。
   - *Trade-off*：要求改部署姿態；架構上真的只能單實例的，殘量交給冷宮的抑制機制（defer）。

2. **客戶面 drain 噪音 = HA-aware 語意分級，不是本案的 liveness 抑制。** 由 [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) 承接：`<db>_up==0` instance 級降 **warning + 可 opt-out**；叢集存活改用 **quorum-aware** 信號（mongo replset state / mariadb-Galera `wsrep_cluster_size`）定 **critical + 不可 opt-out**——drain 期間只要叢集選不出 primary 就必須立刻叫醒。單實例（非 HA）的期待值用**一行 expectation doc**管理（「單實例部署不保證維護期平滑」），**不建 Service Tier 產品系統**（過度）。

3. **重用既有機制、零新常駐元件。** silence / inhibit（[ADR-003](003-sentinel-alert-pattern.md)）+ `_state_maintenance` opt-out（schema `maintenanceMode` + exporter `user_state_filter`）+ `maintenance_scheduler` CronJob（level-triggered，已存在）。**不引入 controller / operator**，延續 [ADR-008](008-operator-native-integration-path.md) v2.10.0 的取消決策。

## 架構冷宮（Deferred Architecture）— `*ExporterAbsent` 抑制機制，設計完成、未建

> **為何保留**：設計已成熟、零成本保存；現場數據顯示其前提（單實例殘量會 fire）**在當前 HA 客戶上不成立**，故 defer。**Reopen-trigger = 第一個非 HA 單實例客戶**（其 `*ExporterAbsent` 殘量在 HA 下不存在的前提失效）。tracked by [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)（status: deferred）。下方保留可直接取回的設計骨架。

**技術路徑：`tenant_metadata_info` anti-join（2 條規則）。** bare `absent()` 會清空 selector 以外所有 label → 產出的告警**沒有 `tenant`**：Alertmanager 層用 `equal:["tenant"]` 做 inhibit 是結構性死路（只能 cluster-wide 抑制），且現行 `*ExporterAbsent` 連「哪個租戶缺席」都分不出（既有缺陷 [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)）。解法是 repo 既有 enforced idiom（pre-commit `check_leftouterjoin_enrichment` 強制）。所有 tenant exporter 共用 `job="tenant-exporters"` → anti-join 是 per-tenant + DB-agnostic，故採 **2 條**取代 4 條 per-DB：

- **規則 A — per-tenant liveness**：`(tenant_metadata_info unless on(tenant) up{job="tenant-exporters"}) unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`。DB 類型進 **label/annotation**（`group_left(db_type, runbook_url, …)`）不進 alertname → 一條 `TenantExporterAbsent` 取代 4 條（否則同租戶各噴一條、3 個標錯 DB）；對齊 kube-prometheus `TargetDown`。生 tenant label 順手修 #869，直接串 `_state_maintenance` opt-out。**防抖**：用 target-existence（`up{job}` 有無），不要 `up==1`；`for: ≥1m`。
- **規則 B — job-level catastrophic**：`absent(up{job="tenant-exporters"})`。anti-join 在「metadata 與 up 同源消失」時自己也空、接不住 → 這條補「整 job 蒸發」。
- **放置**：新 always-on `tenant-liveness` group（非任一 DB pack 條件部署）。

**雙軌維護 opt-out（planned / emergency）。**
- **Planned → config 平面**：`_state_maintenance.expires` / `.recurring`（exporter emit `user_state_filter`，被規則 A 的 `unless` 吃）。慢沒關係，本就提前規劃。
- **Emergency → imperative AM Silence**：「升級卡住、窗正要過期、現在就要延」走 config 平面**太慢**（GitOps + 禁直推 main 要 owner 批，分鐘級）**且**被 observer-paradox 擊穿（發 `user_state_filter` 的 exporter 自己若在 drain target 上就消失）→ 走 **AM Silence API**（秒級、不過 git、不依賴 exporter 存活）。**Max-TTL ≤1h** 強制自動到期（抑制盲區與「HA 降到單副本」最脆弱窗重疊）。
  > **業界依據**：declarative（config / `mute_time_intervals`）vs imperative（Silence API）是兩種用途——Grafana 官方建議 planned/recurring 用 config、ad-hoc 用 silence。雙軌不是二選一。

**兩個 banked 事實修正**（深審查出，留作實作前提）：
- `maintenance_scheduler.extend_silence` 是 **AM-silence 平面**（**非** config plane）；目前只被 CronJob 內部呼叫、**無 CLI 入口**。
- emergency extend 要**新建** `da-tools maintenance extend --tenant <id> --duration <d>`（呼叫 AM Silence API，**非** config 寫入；此 subcommand **目前不存在**），並把指令印進告警 `platform_summary`。

**遷移與規模護欄**（隨機制一起 defer）：
- **Deprecation shim**：4 個舊 alertname 直接消失會靜默斷下游 matcher → 保留一個 release 的 deprecation + CHANGELOG 標。
- **Scale sentinel**：`count without(tenant)(...) > N → MassExporterOutage`（仿平台 pack `DefaultsTruncationStorm` idiom）收斂機房級 page-storm。
- **drift 對賬**：emergency silence 是 stopgap，事後須回填 config，否則把 `silencer_drift_check` 從 advisory 升成 gate（呼應生命週期矩陣 Gap #3）。
- **退役時序引爆**：租戶下線若先刪 exporter、後刪 conf.d → anti-join 對退役租戶噴 critical。正解是 GitOps CI hard-gate：禁止 PR 只刪 K8s target 卻殘留 conf.d（坐實[生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md) 的 RETIRE 0-hard-gate 盲區）。

## Trade-offs（explicit，供日後重評）

- **HA-exporter-first = 把韌性責任放回部署姿態**：是正解非規避；平台可降門檻（預設 HA exporter sidecar chart）。
- **不建子系統 = 自動化程度低於「cordon 自動跟隨」**：接受——殘量窄到不值一個常駐元件及其 silent-failure 面。
- **客戶面交給 #875 而非本案抑制**：接受——現場數據顯示客戶痛點是 HA 粒度，抑制機制解錯題。

## 不做什麼（rejected）

- **Blanket cluster silence**：蓋掉預期外事故，SRE maintenance-window anti-pattern。
- **Cordon-aware 資料面子系統 / 每 recipe topology-join**：殘量窄到不值，over-build，違 ADR-008「不建 controller」。
- **動態生成 per-tenant rule / 自建 operator**：ADR-008 v2.10.0 已取消。
- **為單實例建正式 Service Tier 產品系統**：過度；一行 expectation doc 即可。

## Defer-with-trigger

- **`*ExporterAbsent` 抑制機制（架構冷宮全套）**：defer。Trigger = 第一個非 HA 單實例客戶（[#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870)）。
- **多叢集 cluster-label 抑制**（A 靜音、B/C/D 正常）：defer。Trigger = 實際多叢集部署 + 確認 `cluster` external_label 流到 AM（現 lab 單叢集、無 external_labels）。
- **Taint/cordon-driven 宣告式抑制**：defer。手動 flag 在大規模下會 ops-exhaustion，但 liveness 信號 tenant-keyed（無 `node` 維度），`unless on(node)` dimension-collapse，要 node-scope 須引 `node_owner` 拓撲橋。Trigger = 手動 flag 的規模痛點實際發生。
- **全 CRD-native / operator**：維持 ADR-008 deferred。Trigger = 客戶 RFP 要 kubectl 原生介面；形態 = tenant-api 內嵌 watch-mode，非新 operator。

## 既有缺陷（追蹤中）

- **[#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869) — `*ExporterAbsent` 無 tenant label**：bare `absent()` 清空 label，現在分不出哪個 tenant 缺席。獨立於本題、低優先；冷宮的 anti-join 改寫（隨 #870 或 #875 實作）會順手修掉。

## 外部審查（誠實記錄整段 journey）

1. **設計階段（2 輪 Gemini 對抗式）**：定技術路徑（`absent()` 無 tenant label → Alertmanager 層結構性不可行 → PromQL 層 anti-join）；收緊抑制邊界（補 Max-TTL）；確認「平台自我監控 pack 殘量該由 runbook 預期、不擴大抑制」；認同 cordon 宣告式抑制須先建 `node_owner` 拓撲橋，維持 defer。
2. **實作前深審（workflow + 2 對抗 agent）**：1 條 → 2 條規則（per-tenant 合併 + job-level catastrophic）；config-only → 雙軌維護；補 deprecation shim + `MassExporterOutage` sentinel。blast-radius 量化：合併涉 ~23 檔、7 必改、Alertmanager/Grafana 零改。
3. **2026-06-19 現場數據反轉（最重要的一課）**：上述 1、2 全是「為 `*ExporterAbsent` 殘量設計」。客戶現場數據顯示**痛點不在此**（HA 叢集下殘量不 fire、真痛是 HA mis-tiering）→ **機制進架構冷宮、客戶面改 [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875)**。對抗式 review 能把實作層磨得很細，但**若不用 field data 校驗「這是不是客戶的痛」，再精細的打磨也可能是 over-invest**。

## 關聯

- [ADR-003](003-sentinel-alert-pattern.md) — sentinel + inhibit paradigm
- [ADR-008](008-operator-native-integration-path.md) — operator 取消 / tenant-api watch-mode；本 ADR 延續「不建 controller」
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — custom alerts compiler；absence recipe 已吃 opt-out 的證據
- [ADR-025](025-alerting-plane-self-liveness.md) — alerting-plane liveness / dead-man's-switch；observer-paradox 護欄
- [生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md) — 本 ADR 所屬的 {角色 × 生老病死 × gate} SSOT
- [#875](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/875) — HA-aware 語意分級（承接客戶面 drain 噪音）
- [#870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/870) — `*ExporterAbsent` 抑制機制（架構冷宮，deferred）
- [#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869) — `*ExporterAbsent` 無 tenant label（既有缺陷）
