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

🟡 **Proposed**（2026-06-18；**amend 2026-06-18**）。owner 核可後昇格 Accepted。

> 依語言政策，ADR 自 ADR-019 起不另製 `.en.md`。
> **Amendment（round-3 外審 + workflow/agent 深審）**：技術實作從「1 條 anti-join」修正為 **2 條**（per-tenant 合併 + job-level catastrophic）；維護 opt-out 從「config-plane only」修正為**雙軌**（planned=config / emergency=AM Silence）；修兩個事實錯誤（`extend_silence` 是 AM-silence 平面、`da-tools maintenance extend` CLI 尚不存在）；補 deprecation shim + scale sentinel。詳見各段。

## TL;DR

- **問題**：Kubernetes node 維修 / 多叢集 rolling upgrade 時，想靜音受影響租戶的告警，又不波及其他叢集。
- **結論**：**不需要**一個「維護抑制子系統」。實證顯示乾淨的 drain 對架構正確（HA）的租戶幾乎不產生告警；唯一真正的殘量，是**單實例 exporter 在其節點被 drain 時的 `*ExporterAbsent`（critical）**。
- **決策**：收斂成三件小事 —— (1) 以 **HA exporter 為主**讓殘量自然歸零；(2) 對 liveness 類加一個**窄、需顯式觸發、會自動到期**的維護 opt-out；(3) **全部重用既有機制、零新常駐元件**。

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

**第二類殘量（不同處置）**：平台自我監控 pack（`k8s/03-monitoring/configmap-rules-platform.yaml` 約 10 條：`ThresholdExporterDown`/`TooFewReplicas`/`ConfigReloadStuck`…）無 tenant label、無 opt-out，drain 到承載平台元件的節點時會 fire。但受眾是**平台 SRE**、且多半是「計畫性升級期間**該看到**」的（`TooFewReplicas` = HA 正在降級）→ 處置是**升級 runbook 預期它**，不是抑制。

## 決策：不建子系統，三條 locked decision

1. **HA exporter 為主（最 durable 的解）。** 單實例 exporter 是殘量的根因。推動 HA exporter（≥2 副本 + pod anti-affinity）讓 `absent()` 恆 false、殘量歸零。這是 SRE 正解（不替單點故障掩蓋告警），不是把問題推給租戶。
   - *Trade-off*：要求改部署姿態；架構上真的只能單實例的，殘量交給 decision 2。

2. **對 liveness 類做窄、gated、會自動到期的維護 opt-out（雙軌）。** 僅針對規則 A，由**顯式 maintenance flag** 觸發，文件明寫「這同時會蓋掉真實的 exporter-down」。**Max-TTL ≤1h**：抑制盲區與「HA 降到單副本」最脆弱窗重疊 → 強制自動到期、優先綁實際維護窗。
   - **Planned 維護 → config 平面**：`_state_maintenance.expires` / `.recurring`（exporter emit `user_state_filter`，被規則 A 的 `unless` 吃）。慢沒關係，本就提前規劃。
   - **Emergency extend → imperative AM Silence**：「升級卡住、窗正要過期、現在就要延」這種場景，config 平面**太慢**（GitOps commit→PR→**owner 批**（禁直推 main）→CI→merge→sync→reload→scrape→eval，分鐘級）**且**會被 observer-paradox 擊穿（發 `user_state_filter` 的 exporter 自己若在 drain target 上就消失，bump `expires` 無效）。故 emergency 走 **AM Silence API**（秒級、不過 git、不依賴 exporter 存活）——這正是 `maintenance_scheduler.extend_silence`（AM-silence 平面）已在做的事，但**目前只被 CronJob 內部呼叫、無 CLI 入口**。
   - **要新建** `da-tools maintenance extend --tenant <id> --duration <d>`（呼叫 AM Silence API、**非** config 寫入；此 subcommand **目前不存在**），並把指令印進告警 `platform_summary`（工具即引導）。
   - **事後對賬**：emergency silence 是 stopgap，事後須回填 config（`expires`/`recurring`），否則 drift → 把 `silencer_drift_check` 從 advisory **升成 gate**（emergency silence 超 TTL 未回填 → CI warn；呼應生命週期矩陣 Gap #3）。
   > **業界依據**：declarative（config / `mute_time_intervals`）vs imperative（Silence API）是兩種用途——Grafana 官方建議「planned/recurring 用 config，ad-hoc/即時用 silence」。雙軌不是二選一。

3. **重用既有機制、零新常駐元件。** silence / inhibit（[ADR-003](003-sentinel-alert-pattern.md)）+ `_state_maintenance` opt-out（schema `maintenanceMode` + exporter `user_state_filter`）+ `maintenance_scheduler` CronJob（level-triggered，已存在）。**不引入 controller / operator**，延續 [ADR-008](008-operator-native-integration-path.md) v2.10.0 的取消決策。

### 技術實作：2 條規則 + `tenant_metadata_info` anti-join

bare `absent()` 會清空 selector 以外所有 label → 產出的告警**沒有 `tenant`**：(1) Alertmanager 層用 `equal:["tenant"]` 做 inhibit/silence 是**結構性死路**（只能 cluster-wide 抑制，違反多租戶隔離）；(2) 現行 `*ExporterAbsent` 連「是哪個租戶缺席」都分不出來（既有缺陷 #869）。

解法是 repo 既有 enforced idiom 的 `tenant_metadata_info` anti-join（pre-commit `check_leftouterjoin_enrichment` 強制）。關鍵事實：**所有 tenant exporter 共用 `job="tenant-exporters"` 這一個 scrape job**——anti-join 因此是 per-tenant + DB-agnostic。據此（深審定案）採 **2 條規則**，取代原本 4 條 per-DB 的 `*ExporterAbsent`：

**規則 A — per-tenant liveness（消 4 條冗餘）**

```promql
(
  tenant_metadata_info
  unless on(tenant) up{job="tenant-exporters"}
)
unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
```

- DB 類型進 **label/annotation**（`group_left(db_type, runbook_url, owner, tier)` 從 metadata 帶回），**不進 alertname** → 一條 `TenantExporterAbsent` 取代 `MariaDB`/`PostgreSQL`/`Kafka`/`RabbitMQExporterAbsent`。否則這 4 條 expr 變相同 → 同一缺席租戶各噴一條、3 個標錯 DB（AM 按 alertname 分組、不跨名去重）。對齊業界（kube-prometheus `TargetDown` 即「一條 generic、keyed on topology 而非 per-component」）。
- 生出 tenant label，順手修 #869；直接串既有 `_state_maintenance` opt-out，零新機制。
- **防抖**：用 target-existence（`up{job=...}` 的**有無**），**不要** `up==1`（單次 scrape 失敗即翻 0、rolling drain 瞬間誤觸）；`for: ≥1m`。
- 邊界：`tenant_metadata_info` 對每個 conf.d 租戶無條件 =1（`threshold-exporter/app/collector.go`），故只有「真的不在 conf.d 的租戶」不在偵測範圍（正確）。

**規則 B — job-level catastrophic（anti-join 接不住的 failure mode）**

```promql
absent(up{job="tenant-exporters"})
```

- 「整個 scrape job 一個 target 都沒有」時 fire（機房級蒸發）。anti-join 在「metadata 與 up 同源消失」時自己也空、接不住，故需這條補（對應平台 pack 既有 `ThresholdExporterAbsent` pattern）。

**放置**：合併後規則 DB-agnostic、由共用 job 觸發 → 放**新的 always-on `tenant-liveness` group**（非任一 DB pack 的條件部署、亦不混進 platform-self-monitoring 的「平台 SRE 受眾」語意）。

**Deprecation shim**：4 個舊 alertname 直接消失會靜默斷下游 matcher（既有 silence/routing/dashboard）→ 保留一個 release 的 deprecation（舊名 `severity:none` 或 AM route alias）+ CHANGELOG 標。

**Scale 護欄**：1000 租戶共用 job、機房級事件一次噴 N 條 per-tenant critical → 加 sentinel `count without(tenant)(...) > N → MassExporterOutage`（仿平台 pack 既有 `DefaultsTruncationStorm` 的 `count without(tenant)>50` idiom）收斂 page-storm。

## Trade-offs（explicit，供日後重評）

- **抑制 liveness = 維護窗內對真實 down 盲**：接受（計畫性、窄、gated、會自動到期）。
- **HA-exporter-first = 把韌性責任放回部署姿態**：是正解非規避；平台可降門檻（預設 HA exporter sidecar chart）。
- **不建子系統 = 自動化程度低於「cordon 自動跟隨」**：接受——殘量窄到不值一個常駐元件及其 silent-failure 面。

## 不做什麼（rejected）

- **Blanket cluster silence**：蓋掉預期外事故，SRE maintenance-window anti-pattern。
- **Cordon-aware 資料面子系統 / 每 recipe topology-join**：殘量窄到不值，over-build，違 ADR-008「不建 controller」。
- **動態生成 per-tenant rule / 自建 operator**：ADR-008 v2.10.0 已取消。

## Defer-with-trigger

- **HA-breach load class**（CPU/mem 因負載轉移而爆）：defer。Trigger = 真實 drain 觀測到、且判定為 noise（目前判為「該響」——那是假 HA 的信號）。
- **多叢集 cluster-label 抑制**（A 靜音、B/C/D 正常）：defer。Trigger = 實際多叢集部署 + 確認 edge-eval 下 `cluster` external_label 流到 AM（現 lab 單叢集、無 external_labels）。
- **Taint/cordon-driven 宣告式抑制 + 平台 pack rollout-aware 降級**：defer。手動 flag 在大規模下會 ops-exhaustion，metric-driven 是對的方向；但本案 liveness 信號是 tenant-keyed（anti-join 後沒有 `node` 維度），`unless on(node)` 接不上（dimension-collapse），要 node-scope 須引 `node_owner` 拓撲橋——那正是刻意 defer 的複雜度。Trigger = 手動 flag 的規模痛點實際發生（見「已知限制」的兩個 landmine，它們都指向這同一塊 pipeline/state-driven 信號）。
- **全 CRD-native / operator**：維持 ADR-008 deferred。Trigger = 客戶 RFP 要 kubectl 原生介面；形態 = tenant-api 內嵌 watch-mode，非新 operator。

## 已知限制與既有缺陷

- **Max-TTL 懸崖**（見 decision 2）：升級超時 → 告警湧入；**雙軌維護**解決——emergency 走 AM Silence（秒級、不依賴 exporter）即時止血、事後回填 config。（先前版本誤把緩解掛在 config-plane 的 `extend_silence`；深審修正：`extend_silence` 是 AM-silence 平面。）
- **退役時序引爆（offboarding ordering）**：若租戶下線時**先刪 exporter 部署、後刪 conf.d**（或 GitOps 同步有時間差），則 `up` 已斷、`tenant_metadata_info` 還在 → anti-join 對一個**正在退役**的租戶噴 critical。這坐實了[生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md)裡「RETIRE 階段 0 條 hard-gate」的盲區；正解是一條 GitOps CI hard-gate：**禁止 PR 只刪 K8s target 卻殘留 conf.d**（強制 conf.d 先移、或兩者同移，先切斷 metadata 源）。
- **`*ExporterAbsent` 既有缺陷**：bare `absent()` 無 tenant label → 現在分不出哪個 tenant 缺席。獨立於本題；上述 anti-join 改寫順手修掉它（另案實作）。

## Blast-radius 護欄

decision 2 引入「會自主關掉 critical 告警」的能力，護欄：

- **gated**：顯式 maintenance flag，非預設開啟；
- **會自動到期**：`maintenance_scheduler` 的 `endsAt`（dead-man's-switch）；
- **observer-paradox**：若 exporter 自己就在被 drain 的節點上 → `user_state_filter` 隨之消失 → opt-out 失效（恰在需要時）→ 須把監控面 pin 在 drain target 之外（與 [ADR-025](025-alerting-plane-self-liveness.md) 的 liveness 同源風險）；
- **套用前可檢視**：`blast_radius` diff + `silencer_drift_check` 收尾（皆已存在）。

## 外部審查（2 輪 Gemini + 實作前 workflow/agent 深審）

兩輪外部對抗式審查改變了三件事，並背書 promote：

1. **技術路徑定案**：審查確認 Alertmanager 層抑制因 `absent()` 無 tenant label 而**結構性不可行**，逼向 PromQL 層的 `tenant_metadata_info` anti-join（即上方技術實作）。
2. **抑制邊界收緊**：補上 Max-TTL（HA 降級窗的盲區風險），並確認「平台自我監控 pack 殘量該由 runbook 預期、不該擴大抑制」。
3. **確認該 defer 的就 defer**：審查一度提議用 `unless on(node)` 做宣告式 cordon 抑制，但這與 anti-join 的 tenant-keyed 信號**維度衝突**（dimension-collapse）——審查最終認同此路須先建 `node_owner` 拓撲橋，維持 defer 是誠實且正確的。

審查也標出兩個規模化 landmine（Max-TTL 懸崖、平台 pack 的「狼來了」習慣化），二者都指向同一塊已 defer 的 pipeline/state-driven 信號——它們實際發生時，就是建那塊的具體 trigger。

**實作前深審（workflow + 2 個對抗 agent，2026-06-18 amend）** 又改了三件，已納入上方各段：

4. **1 條 → 2 條**：anti-join 是 per-tenant + DB-agnostic，4 條 per-DB 規則 expr 變相同（冗餘 + 標錯 DB）→ 合併成 `TenantExporterAbsent` + 補一條 job-level `absent(up{job})` 接「整 job 蒸發」（anti-join 接不住）。
5. **config-only → 雙軌維護**：emergency extend 走 config 平面太慢 + 被 observer-paradox 擊穿 → 改 imperative AM Silence；修 `extend_silence` 平面誤述 + `da-tools maintenance extend` CLI 尚不存在兩個事實錯誤。
6. **補遷移與規模護欄**：deprecation shim（4 舊名一個 release）+ `MassExporterOutage` sentinel（防 page-storm）。

> blast-radius 量化（agent）：合併涉 ~23 檔、7 必改、Alertmanager/Grafana 零改、命名 `TenantExporterAbsent` 命中 `.pint.hcl` 既有豁免；外部成本是既有 silence/routing 斷 → shim 緩解。

## Day-3 / 規模化 roadmap（out-of-scope，列入 radar）

1. **SLO / burn-rate recipe（症狀 > 原因）**：最深的 lever——告警若是 SLO 型，乾淨 drain 不破 SLO 就根本不 page，本題大半蒸發。roadmap 第一順位。
2. **控制面 per-tenant 解耦編譯**：合理的 scale 顧慮；tenant-api 已有 shift-left per-tenant 驗證、已在 deep-water radar。
3. **Auto-quarantine GC**（殭屍告警自動回收）：對應生命週期矩陣 RETIRE 段的缺口；偏好 **auto-mute + notify**（auto-delete 屬高 blast-radius，須 hard-gate）。

## 關聯

- [ADR-003](003-sentinel-alert-pattern.md) — sentinel + inhibit paradigm
- [ADR-008](008-operator-native-integration-path.md) — operator 取消 / tenant-api watch-mode；本 ADR 延續「不建 controller」
- [ADR-023](023-write-plane-single-writer-invariant.md) — single-writer 寫入面護欄
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — custom alerts compiler；absence recipe 已吃 opt-out 的證據
- [ADR-025](025-alerting-plane-self-liveness.md) — alerting-plane liveness / dead-man's-switch；observer-paradox 護欄
- [生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md) — 本 ADR 所屬的 {角色 × 生老病死 × gate} SSOT
- `_state_maintenance` / `user_state_filter` opt-out — schema `maintenanceMode` + threshold-exporter `collector.go`（本 ADR 擴其覆蓋到 liveness class）
