---
title: "ADR-026: Node/Cluster 維護告警抑制 — Liveness-Class Gap，不是子系統"
tags: [adr, alerting, maintenance, k8s]
audience: [platform-engineers, sre]
version: v2.10.0
lang: zh
id: ADR-026
tracking_kind: adr
status: proposed
domain: k8s
created_at: 2026-06-18
updated_at: 2026-06-18
---

# ADR-026: Node/Cluster 維護告警抑制 — Liveness-Class Gap，不是子系統

## 狀態

🟡 **Proposed** (2026-06-18) — 經 Gate 1 靜態實證收斂 + 兩輪外部 adversarial review（Gemini）。owner gate 後昇格 Accepted。

> 依語言政策不另製 `.en.md`（ADR-019/020 起 EN mirror 慣例停止）。

## 背景

起點是一個 day2 ops 問題：**Kubernetes node 維修 / 多叢集 rolling upgrade 時，想抑制受影響 tenant 的告警，同時其他叢集保持正常。** 探索把問題從「node-level 靜音」抬高到真正目的——**讓「預期內」的擾動對人隱形，同時讓「預期外」的擾動照樣可見**（blanket silence 用犧牲後者換前者，不通過此目的）。

用 **Gate 1（靜態實證，零叢集成本）** 逐條過「乾淨 drain 期間會 fire 什麼」，對每條查 `for:` / severity / routing / 是否吃 `_state_maintenance` opt-out：

- **cordon ≠ NotReady**、**graceful evict ≠ crash** → `NodeNotReady`（`rule-packs/rule-pack-kubernetes.yaml`，for:3m）只在後續 reboot 階段 node 真的 down >3min 才 fire；`ContainerCrashLoop` 乾淨 drain 不 fire。
- **閾值類吃 opt-out**：`MariaDBHighConnections` 帶 `unless on(tenant)(user_state_filter{filter="maintenance"}==1)`。
- **Custom recipe（含 `absence`）也吃 opt-out**：編譯器無條件注入（`scripts/tools/dx/custom_alerts/recipes.py`，absence 分支 fall-through 確認），且因 self-scoped 在帶 tenant 的 `custom:threshold:{id}` 而能 join。
- **唯一不可抑制殘量 = 平台 liveness 類**：`*ExporterAbsent`（`absent(<up_metric>{job="tenant-exporters"})`，critical，for:30s）**無 opt-out**，且**只在單實例 exporter 上 fire**（HA ≥2 副本 → `absent()` false → 不 fire）。
- **Routing**：lab 為 log-only 空 receiver；prod = tenant 自配 receiver，**無 severity-based page/FYI 分流** → critical 殘量會 page（若 tenant 配了 receiver）。
- **技術 wrinkle**：bare `absent()` 只帶 selector 的 `=` matcher label → **無 `tenant` label**。

### Gate 1 判決

> **narrow PARTIAL。** 乾淨 drain 的唯一不可抑制殘量 ＝ **平台 `*ExporterAbsent` 類 × 單實例 exporter**（critical）。HA exporter 殘量≈0；閾值類與 custom recipe 皆已可 opt-out。**「cordon-aware 維護抑制子系統」over-build，不做。**

精確化：乾淨 drain 時 exporter pod 被 evict → `<up_metric>` series 缺席 → 觸發 `absent()` 的 `*ExporterAbsent`，**不是** `<db>Down`（`_up==0` 需 series 在場為 0）。租戶側殘量收斂到**一個 pattern：`*ExporterAbsent`**。

### 殘量第二類：平台自我監控 pack

`k8s/03-monitoring/configmap-rules-platform.yaml` 約 10 條平台自我監控告警（`ThresholdExporterDown`/`Absent`/`TooFewReplicas`、`ConfigReloadStuck`、`TenantApiSSEReconnectFailure`…）**無 tenant label、無 maintenance opt-out**——drain 到承載平台自身元件（exporter / tenant-api / Prometheus）的 node 時會 fire。

但這是**不同 persona、不同處置**：受眾是平台 SRE，而且**多半是「計畫性升級期間預期該看到」的**（`ThresholdExporterTooFewReplicas` for:5m = HA 降級，真實且你想知道）。故處置 = **升級 runbook 預期它 + 既有 Watchdog 外部 DMS 守「平台真死」**，**不是**抑制。（外審原舉的 `container_last_seen`/`KubePodNotReady` 在本專案不成立：recipe 受限產不出前者；NodeNotReady 經 `node_owner` 已吃 opt-out。）

## 決策

**不建 cordon-aware 維護抑制子系統。** 三條 locked decision：

1. **HA exporter 為主（最 durable）。** 單實例 exporter 是殘量根因；HA exporter（≥2 副本 + pod anti-affinity）讓 `absent()` 恆 false、殘量歸零。SRE 正解（別替單點故障掩蓋告警），非規避。
   - *Trade-off*：要求改部署姿態；真的只能單實例者留殘量 → decision 2。

2. **平台 liveness 類做 gated、明寫 trade-off 的 maintenance-aware 抑制。** 僅針對 `*ExporterAbsent`（必要時含 `<db>Down`），由**顯式 maintenance flag** 觸發，文件明寫「這同時會蓋掉真實 exporter-down」。
   - *Trade-off*：維護窗內對該 tenant 真實缺席盲。接受（計畫性 + 窄 + gated + window-bound）。
   - **Max-TTL ≤1h**：盲區與「HA 降到單副本」最脆弱窗重疊 → flag 強制 Max-TTL、優先 window-bound 綁實際維護窗。
   - **已知限制（Max-TTL 懸崖）**：升級卡住超時 → 告警湧入、ops 一邊修一邊被轟。便宜緩解 = 一鍵延長（重用 `maintenance_scheduler` 既有 `extend_silence`）；結構解見 Defer。

3. **重用既有機制，零新常駐元件。** silence / inhibit（[ADR-003](003-sentinel-alert-pattern.md)）+ `_state_maintenance` opt-out（tenant-config schema `maintenanceMode` + exporter `user_state_filter`）+ `maintenance_scheduler` CronJob（level-triggered，已存在）。**不引入 controller / operator**（延續 [ADR-008](008-operator-native-integration-path.md) v2.10.0 取消決策）。

### 技術實作：`tenant_metadata_info` anti-join（外審定案 A）

bare `absent()` 清空 selector 以外所有 label → 告警 100% 無 tenant。據此 **Alertmanager 層 inhibit/silence by tenant 結構性死路**（AM 無 tenant → `equal:["tenant"]` 失維度基石 → 只能 cluster-wide，違多租戶隔離）。改 **PromQL 層 `tenant_metadata_info` anti-join**（repo 既有 enforced void pattern，pre-commit `check_leftouterjoin_enrichment` 強制）：

```promql
(
  tenant_metadata_info
  unless on(tenant) (up{job="tenant-exporters"} == 1)
)
unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
```

優勢：① 生出 tenant label，**順手修掉既有「分不出哪個 tenant 缺席」缺陷**；② 直接重用 `_state_maintenance` opt-out，零新機制、零架構污染。邊界：`tenant_metadata_info` 無條件每 tenant=1（`components/threshold-exporter/app/collector.go`），僅「真的不在 conf.d 的 tenant」不在偵測範圍（正確）。代價：每個 DB pack 的 `*ExporterAbsent` 從 `absent()` 改寫為 anti-join（有 lint 護航）。

## Trade-offs（explicit，供重新評估）

- **抑制 liveness = 維護窗內對真實 down 盲**：接受（計畫性、窄、gated、有界）。
- **HA-exporter-first = 責任落部署姿態**：正解非規避；平台可降門檻（預設 HA exporter sidecar chart）。
- **不建子系統 = 自動化低於「cordon 自動跟隨」**：接受，殘量窄到不值常駐元件 + 其 silent-failure 面。

## Defer-with-trigger

- **HA-breach load class**（CPU/mem 因負載轉移爆）：defer。Trigger = 真實 drain 觀測到且判為 noise（現判 (iii) 該響——假 HA 信號）。
- **多叢集 cluster-label 抑制**（A 靜音、B/C/D 正常）：defer。Trigger = 實際多叢集 + 確認 edge-eval 下 `cluster` external_label 流到 AM（現 lab 單叢集、無 external_labels）。
- **Taint/cordon-driven 宣告式抑制 + 平台 pack rollout-aware 降級**：defer。手動 flag 規模下會 ops-exhaustion，metric-driven 是對方向；但 tenant liveness 是 tenant-keyed（anti-join 後無 `node`），`unless on(node)` 接不上（dimension-collapse），須 `node_owner` 拓撲橋（刻意 defer 的複雜度）。**外部審查 round-2 的兩個 landmine（Max-TTL 懸崖、平台 pack 狼來了）都指向這同一塊 pipeline/state-driven 信號**——它們實際 bite = 本 defer 的具體 trigger（屆時一併處理 node-scope + rollout-aware 把 critical 降級 info）。
- **全 CRD-native / operator**：維持 [ADR-008](008-operator-native-integration-path.md) deferred。Trigger = 客戶 RFP kubectl-native；形態 = tenant-api 內嵌 watch-mode，非新 operator。

## 既有缺陷（另開追蹤）

- **`*ExporterAbsent` 的 tenant label 為空**（bare `absent()` 只留 `=` matcher）→ 現在分不出哪個 tenant 缺席。獨立於本題的既有缺陷；技術實作的 anti-join 改寫**順手修掉它**。

## Blast-radius / failure mode

decision 2 是「會自主關掉 critical 告警」的能力，護欄：

- **gated**（顯式 maintenance flag，非預設）；
- **window-bound / dead-man's-switch**（`maintenance_scheduler` `endsAt` 過期自動恢復）；
- **observer-paradox**：exporter 自己若在 drain target 上 → `user_state_filter` 消失 → opt-out 失效（恰在需要時）→ 須 pin 監控面 off drain target（[ADR-025](025-alerting-plane-self-liveness.md) liveness 同源風險）；
- **plan-before-apply**：`blast_radius` diff + `silencer_drift_check` 收尾（皆已存在）。

## 替代方案（rejected）

- **Blanket cluster silence**：rejected——蓋掉預期外事故，SRE maintenance-window anti-pattern。
- **Cordon-aware data-plane 子系統 / 每 recipe topology-join**：rejected——殘量窄到不值，over-build，違 ADR-008「不建 controller」。
- **動態生成 per-tenant rule / 自建 operator**：rejected——ADR-008 v2.10.0 已取消。

## 外部審查紀錄（2 輪 Gemini adversarial，take/reframe/reject）

- **Challenge 3（absent() 無 tenant → A vs B）**：定案 **A**（`tenant_metadata_info` anti-join，repo 既有 idiom）；B 結構性否決。
- **Challenge 1（HA 盲區）**：take → Max-TTL ≤1h + window-bound。
- **Challenge 2（殘量判定）**：外審例子本專案不成立（已勘誤）；但坐實「平台自我監控 pack」殘量 → runbook-not-suppress。
- **Challenge 4（taint-driven 宣告式）**：方向 take、機制 reject——它與自身 anti-join 的 tenant-keyed 信號 dimension-collapse 衝突；外審 round-2 認輸並背書 defer。
- **Round-2 兩 landmine**（Max-TTL 懸崖、平台 pack 狼來了）：take 為 defer 精煉，皆指向同一塊 deferred state-driven 信號。
- **Round-2 結論**：強烈建議 promote。

## Day-3 / 規模化 roadmap（out-of-scope 本 ADR，入 radar）

1. **SLO / burn-rate recipe（症狀 > 原因）**：最深 lever——告警若為 SLO 型，乾淨 drain 不破 SLO 就不 page，本題大半蒸發。roadmap 第一順位。
2. **控制面 per-tenant 解耦編譯**：合理 scale 顧慮，但 tenant-api 已有 shift-left per-tenant 驗證、已在 deep-water radar；reframe 為 radar。
3. **Auto-quarantine GC**（殭屍告警自動回收）：對應生命週期矩陣的死/RETIRE 段缺 gate；偏好 **auto-mute + notify**（auto-delete = 高 blast-radius，須 hard-gate）。

## 關聯

- [ADR-008](008-operator-native-integration-path.md) — operator 取消 / tenant-api watch-mode；本 ADR 延續「不建 controller」
- [ADR-003](003-sentinel-alert-pattern.md) — sentinel + inhibit paradigm
- [ADR-023](023-write-plane-single-writer-invariant.md) — single-writer 寫入面護欄
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — custom alerts compiler；absence recipe 已吃 opt-out 的證據
- [ADR-025](025-alerting-plane-self-liveness.md) — alerting-plane liveness / dead-man's-switch；observer-paradox 護欄
- [生命週期治理矩陣](../internal/monitoring-lifecycle-governance-matrix.md) — 本 ADR 所屬的 {角色 × 生老病死 × gate} SSOT
- `_state_maintenance` / `user_state_filter` opt-out — tenant-config schema `maintenanceMode` + threshold-exporter `collector.go`（本 ADR 擴其覆蓋到 liveness class）
