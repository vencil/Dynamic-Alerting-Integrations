---
title: "ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: zh
id: ADR-006
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-03-16
updated_at: 2026-11-21
---
# ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N)

> **Language / 語言：** **中文 (Current)** | [English](./006-tenant-mapping-topologies.en.md)

## 狀態

✅ **Accepted** (v2.1.0) — 工具鏈已完成，1:N 端到端整合待生產驗證
📎 **Addendum** (v2.10.0) — 基礎設施指標（disk/PVC/node）的租戶歸屬政策，見下方 §Addendum
🧭 **修訂** (2026-11-21) — 多叢集同租戶的叢集身分 `da_cluster`（由每台 Prometheus 的 external label 提供）與逐叢集的告警粒度，見下方「多叢集同租戶」；實作進行中

## 背景

企業環境中，資料庫實例與租戶之間的映射關係並非總是一對一。隨著平台規模擴大，我們遇到三種常見的拓撲模式：

### 拓撲分類

| 拓撲 | 說明 | 典型場景 |
|:-----|:-----|:---------|
| **1:1** | 一個 namespace/實例對應一個租戶 | 獨立部署的微服務、專用 DB |
| **N:1** | 多個 namespace/實例聚合為一個租戶 | 多叢集同租戶、DR 對、讀寫分離 |
| **1:N** | 單一實例內含多個邏輯租戶 | Oracle 多 schema、DB2 多 tablespace、共享 RDS |

### 問題陳述

目前平台的 1:1 映射是隱式的（`namespace` label = tenant ID），N:1 已透過 `scaffold_tenant.py --namespaces` 的 `relabel_configs` regex 實現。但 **1:N 拓撲**缺乏原生支援：

- 單一 DB 實例的 metrics 中僅包含 `instance` label，不含租戶維度
- Exporter 無法感知實例內部的 schema/tablespace 劃分
- 告警規則無法區分同一實例上不同租戶的閾值

需要一個通用方案將實例級指標拆分為租戶級指標，而不修改 threshold-exporter 核心邏輯。

## 決策

**在資料平面 (Data Plane) 透過 Prometheus Recording Rules 解決映射，控制平面 (Control Plane) 的 threshold-exporter 保持零變更。**

具體機制：

1. **1:1（預設）**：維持現行 `namespace` label = tenant ID 的隱式映射
2. **N:1（已實現）**：Prometheus `relabel_configs` regex 聚合多個 namespace 至單一 tenant label
3. **1:N（新增）**：Config-driven `instance_tenant_mapping` → 自動產生 Rule Pack Part 1 Recording Rules

### 1:N 實現架構

```yaml
# _instance_mapping.yaml — 新配置檔（位於 config-dir）
instance_tenant_mapping:
  oracle-prod-01:
    - tenant: db-a
      filter: 'schema=~"app_a_.*"'
    - tenant: db-b
      filter: 'schema=~"app_b_.*"'
  db2-shared-01:
    - tenant: db-c
      filter: 'tablespace="ts_client_c"'
    - tenant: db-d
      filter: 'tablespace="ts_client_d"'
```

```yaml
# 自動產生的 Recording Rule (Rule Pack Part 1: Data Normalization)
groups:
  - name: tenant_mapping_oracle-prod-01
    rules:
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_a_.*"}
        labels:
          tenant: db-a
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_b_.*"}
        labels:
          tenant: db-b
```

## 基本原理

### 為何選擇資料平面映射

**Exporter 零變更**：threshold-exporter 的職責是「YAML → Metrics」，不應承擔實例到租戶的映射邏輯。映射屬於資料正規化，應在 Prometheus 層級處理。

**Recording Rule 的天然優勢**：
- 產生的時間序列自帶 `tenant` label，後續的告警規則、Dashboard 查詢無需任何修改
- TSDB 同時保留原始實例指標與映射後的租戶指標，支援雙視角分析
- Recording Rule 的計算在 Prometheus 評估週期內完成，無額外延遲

**統一管理**：三種拓撲的最終表現一致 — 所有告警規則都能假設 `tenant` label 存在。

### 為何拒絕在 Exporter 中解決

- **職責越界**：Exporter 變成了「資料正規化引擎」，違反單一職責
- **重啟成本**：映射變更需重啟 exporter，影響所有租戶的指標採集
- **多 exporter 同步**：HA 部署下兩個 exporter 需一致映射，增加分佈式一致性問題

### Why Reject Solving in Alertmanager

- **太晚**：Alertmanager 只處理已觸發的告警，無法在指標層級進行拆分
- **Dashboard 盲區**：Grafana 等工具直接查詢 Prometheus，繞過 Alertmanager

## 後果

### 正面影響

✅ threshold-exporter 完全不感知拓撲複雜度，保持簡潔
✅ 三種拓撲透過統一的 `tenant` label 收斂，下游規則和 Dashboard 零修改
✅ Recording Rule 支援熱更新（config-reloader sidecar，#1246 才真正補上部署；在那之前需人工 reload），無需重啟 Prometheus
✅ TSDB 同時保留原始與映射後指標，支援多維度回溯分析
✅ 與現有 Rule Pack 架構（ADR-005 Projected Volume）完美整合

### 負面影響

⚠️ Recording Rule 產生額外時間序列，增加 TSDB 存儲 (~2× per mapped metric)
⚠️ 需開發 `generate_tenant_mapping_rules.py` 工具自動產生 Recording Rules
⚠️ 1:N 映射的 filter 語法需租戶理解底層 DB 的 schema/tablespace 結構

### 運維考量

- `generate_tenant_mapping_rules.py` 產出為 Rule Pack Part 1 ConfigMap，走 Projected Volume 分發
- CI 驗證：映射中引用的 tenant ID 必須在 `config-dir` 中存在對應的 tenant YAML
- Cardinality 評估：每個映射 entry 產生 M 條 recording rules（M = mapped metric 數量），需評估對 TSDB 的影響
- 建議為 1:N 租戶設定 `_cardinality_multiplier` 標記，供 capacity planning 參考

## 替代方案考量

### 方案 A：Exporter 內建映射 (已拒絕)
- 優點：單一元件處理
- 缺點：職責越界、重啟影響大、HA 一致性問題

### Approach B: Alertmanager-Level Mapping (Rejected)
- 優點：僅影響通知路徑
- 缺點：指標層級無法拆分、Dashboard 盲區

### 方案 C：外部 Proxy (已考量)
- 優點：完全解耦
- 缺點：引入新元件、延遲增加、運維複雜度高

## v2.1.0 Implementation Summary

- `generate_tenant_mapping_rules.py` — 從 `_instance_mapping.yaml` 自動產生 Recording Rules，支援 Oracle/DB2/通用 filter 語法（36 tests）
- `discover_instance_mappings.py` — 自動偵測 Prometheus 中的實例拓撲（1:1/N:1/1:N），輸出建議映射配置
- `scaffold_tenant.py --topology=1:N` — Onboarding 整合（含 `--mapping-instance`, `--mapping-filter`）
- 範例配置 `conf.d/examples/_instance_mapping.yaml`
- Go/Python 雙端 reserved key 同步

## 演進狀態

- **v2.1.0**（已完成）：核心工具鏈（generate_tenant_mapping_rules / discover_instance_mappings / scaffold_tenant --topology=1:N）
- **v2.1.0**（已完成）：Federation 場景 B（`rule-pack-split`）支援邊緣/中央分層架構下的映射行為

**殘留**：
- 在實際多 schema Oracle 環境驗證端到端流程（待生產環境回饋）
- Schema validation（`_instance_mapping.yaml` JSON Schema）排入後續

## 多叢集同租戶：叢集身分與告警粒度

**決策：同一個租戶可以出現在多個叢集。每台 Prometheus 用一個 external label `da_cluster` 說明自己在哪個叢集；凡是以租戶 exporter 序列為輸入的規則，聚合與比對都以 (tenant, da_cluster) 為單位，告警也逐叢集各發一次。**

這一節是上表 N:1「多叢集同租戶、DR 對（主站與備援站各一套）」的契約。**它描述的是完成後的行為，目前只完成一部分**：rule pack 已經逐叢集計算；逐叢集的存在判定（第 6 點）與 Alertmanager 的分組、去重（第 5 點）還沒做。實作分階段進行，進度見 [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)。

### 為什麼需要

規則裡有兩類寫法在同租戶跨叢集時會出錯：

- **互相遮蔽。** 存活判定寫成「預期存在的實例，扣掉有成功回報的實例」，兩邊以 `on(tenant, instance)` 配對。兩個叢集各有一台位址相同的實例時（各叢集內部網段重複很常見），健康的那台會把故障的那台消掉，故障的叢集不告警。
- **悄悄合併。** `sum by(tenant)` 會把兩個叢集加在一起，`max by(tenant)` 只留下較大的那個。值班人員看不出是哪個叢集，數值也不再對應任何一個資料庫。

### 契約

1. **叢集身分的來源。** 每台 Prometheus 在 `global.external_labels` 設 `da_cluster: <叢集 ID>`，一台一行，抓取 job 不必寫任何東西。external label 是 Prometheus 在資料離開它時加上的——送往 Alertmanager 的告警、`/federate`、remote write 都會帶。所以兩個叢集的序列或告警相遇的地方（中央 Prometheus、Alertmanager）看到的資料已經分得出叢集；只評估自己叢集的 Prometheus，序列上沒有這個 label，規則的分組自然等同逐租戶。單一叢集的部署也要設，值固定即可。
2. **叢集 ID。** 在整個平台內唯一，每個叢集一個、不隨時間改變；同一個叢集裡的 Prometheus 副本（HA 對）用同一個值。已經照 [Federation 整合指南](../integration/federation-integration.md) 設了 `external_labels.cluster` 的部署，`da_cluster` 用同一個值、與它並列。
3. **閾值仍是租戶一份。** 閾值、維護狀態、租戶 metadata 由 threshold-exporter 送出，一個租戶一組，不分叢集。這些序列若是從別的叢集 federate 上來，身上也會帶 `da_cluster`；規則一律先以 `by(tenant)` 收成一份，再用 `on(tenant) group_left` 套到該租戶的每個叢集（`group_left` 表示右邊一條序列可以配左邊多條），所以帶不帶結果相同。前提是各叢集送上來的閾值內容一致。
4. **規則的寫法。** 輸入是租戶 exporter 序列的聚合，`by(...)` 只要含 `tenant`、`instance`、`pod`、`namespace`、`node` 任一個，就必須同時含 `da_cluster`。兩邊都是這類序列的配對（`on(...)`）也一樣。
5. **告警帶 `da_cluster`。** 送到 Alertmanager 的告警都有這個 label：規則算出來的告警若已經帶著（它讀的序列來自哪個叢集），就是那個值；沒有的，Prometheus 送出時補上自己的 external label，也就是「評估這條規則的那台 Prometheus 所在的叢集」。Alertmanager 的分組與 warning／critical 去重（[ADR-001](./001-severity-dedup-via-inhibit.md)）都把它納入，兩個叢集的同名告警互不壓制。只讀 threshold-exporter 序列的告警（例如「租戶沒設定閾值」）屬於後一種：它講的是整個租戶，label 上的叢集只代表是哪一台算出來的。
6. **「該有卻沒有」逐叢集判定。** 租戶可以在設定裡宣告自己預期出現在哪些叢集；有宣告時，任一個宣告的叢集沒有回報就告警。沒宣告時，只要在至少一個叢集出現就算存在。
7. **沒設 `da_cluster` 的 Prometheus 在上線時被指出來。** `da-tools federation-check edge` 讀實際那台 Prometheus 的設定，`external_labels.da_cluster` 沒有值就失敗；本 repo 自己的設定由抓取設定的 lint 檢查同一件事。兩者都只看一個地方，不需要推算 relabel 的結果。

### 範例

每台 Prometheus 的設定（各自的值）：

```yaml
global:
  external_labels:
    da_cluster: edge-a
```

同一個租戶 `t1` 在 `edge-a`、`edge-b` 各有一台位址同為 `10.0.0.5:50000` 的資料庫，`edge-a` 那台故障。中央 Prometheus 從兩個叢集收到的序列：

```text
db_expected{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}  1
db_expected{tenant="t1", da_cluster="edge-b", instance="10.0.0.5:50000"}  1
db_up{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}        0
db_up{tenant="t1", da_cluster="edge-b", instance="10.0.0.5:50000"}        1
```

配對不含叢集身分——沒有結果，不告警（`edge-b` 的成功把 `edge-a` 遮掉）：

```promql
max by(tenant, instance) (db_expected)
  unless on(tenant, instance)
max by(tenant, instance) (db_up == 1)
```

配對含叢集身分——一筆結果 `{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}`：

```promql
max by(tenant, da_cluster, instance) (db_expected)
  unless on(tenant, da_cluster, instance)
max by(tenant, da_cluster, instance) (db_up == 1)
```

`db_expected` 與 `db_up` 是為了說明而取的名字，不是平台的指標。

### 後果與已知限制

- **升級順序不拘。** 規則在沒有 `da_cluster` 的序列上照樣運作，結果與不分叢集時相同；external label 設上去之後，離開那台 Prometheus 的資料才開始帶它。
- **告警在 Alertmanager 那一端多一個 label。** 設上 external label 之後，Alertmanager 把帶新 label 的告警視為另一條：新的立刻出現並通知一次，舊的那條要等到它過期才消失，中間兩條並存。
- **既有的 silence 仍然有效。** silence 以它列出的 label 比對，告警多一個 label 不影響。
- **在邊緣叢集本地看不到叢集。** 邊緣 Prometheus 的查詢、`ALERTS` 序列、告警 annotation 裡的 `$labels.da_cluster` 都是空的；值在資料到了 Alertmanager 或中央之後才有。
- **一台 Prometheus 直接抓好幾個叢集時不適用。** external label 只有一個值。這種部署要自己在各叢集的抓取 job 上寫 `da_cluster`（序列上已有的值優先於 external label），lint 不檢查那個寫法。
- **各叢集的閾值要一致。** 每個叢集各有一份 threshold-exporter 並 federate 到中央時，中央以 `by(tenant)` 把它們收成一份，取其中較大的值；內容不一致時，中央用的不是任何一個叢集自己的那組。
- **有些抓取代理在抓取時就加上 external label**（不是 Prometheus 本身的行為）。那樣的話序列在本地就帶著 `da_cluster`，規則的結果相同。
- **加總是逐叢集的。** 「該租戶所有實例加總」的規則（例如鎖等待、deadlock 次數）在每個叢集各自加總、各自比對閾值。
- **維護模式是整個租戶。** 無法只對一個叢集進維護。
- **各叢集不能有不同的閾值。** 同一個租戶在所有叢集用同一組閾值。

### 考慮過的替代方案

- **沿用 `cluster` 這個名字。** RabbitMQ、Elasticsearch 的 exporter 自己就輸出 `cluster`（該服務自己的叢集名），而 Prometheus 遇到同名 label 有兩種行為，兩種都會出錯。預設情況下保留抓取設定寫的值、把 exporter 的改名為 `exported_cluster`，讀 exporter 那個 `cluster` 的規則就讀到平台的叢集 ID。設了 `honor_labels: true` 時（中央從邊緣拉資料的 federation job 就是）保留 exporter 的值，叢集身分被換成服務自己的叢集名。所以用平台專屬的名字。
- **在每個抓取 job 用 relabel 寫入 `da_cluster`。** 這是本節最初的做法，好處是規則評估時序列上就有值。但要寫的地方有四處（租戶 exporter、容器指標、volume 指標、Pod 狀態指標），其中一處還得限定在單一 Service 上；只寫了一部分時，需要把兩種來源配對的容器告警會配不上而靜默。要在部署前擋下這種設定，檢查工具就得靜態推算 relabel 的結果，等於重做一遍 Prometheus 的行為，補不完。而規則真正需要叢集身分的地方只有兩個叢集的資料相遇之處，那裡的資料已經帶著 external label。
- **只在已經帶 `instance` 這類 label 的地方加 `da_cluster`，租戶層級的聚合維持跨叢集一份。** 要改的地方少得多。但規則分層部署時（[ADR-004](./004-federation-central-exporter-first.md)、[Federation 整合指南](../integration/federation-integration.md) 的「邊緣評估」一節），租戶層級的 recording rule 是在各邊緣叢集算的：兩個邊緣各送一條同名序列到中央，結果與「全部在中央評估」時不同。逐叢集是兩種部署方式下結果一致的最粗粒度。比值類的規則也無法把各邊緣的比值再合成整個租戶的比值。
- **不支援，並擋下跨叢集的租戶。** 要擋也得先有叢集身分；而 DR 對與多地部署正是 N:1 列出的典型場景。

## Addendum：基礎設施指標的租戶歸屬 (v2.10.0)

**決策：磁碟、PVC、節點這類基礎設施指標，只支援 1:1 的租戶歸屬（一個 namespace = 一個租戶）。**

ADR-024 的自助告警開始讓租戶對平台的基礎設施指標（例如 kubelet 的 `kubelet_volume_stats_*`）設條件。這些指標原生只帶 `namespace`、不帶 `tenant`，所以在抓取階段直接把 `namespace` 當成 `tenant`——沿用本 ADR 既有的 1:1 做法。

**為什麼 N:1 與 1:N 不在這次的支援範圍。** 本 ADR 的 N:1（多個 namespace 聚合成一個租戶）和 1:N（一個實例切成多個邏輯租戶）靠的是額外的映射規則，而基礎設施指標走的是抓取階段的直接改名、拿不到那層映射：

- **N:1 會靜默漏告警。** 指標被標成 `tenant="db-a-read"`，但租戶的設定用的是 `db-a` 這個名字——兩者對不上，告警永遠不觸發。
- **1:N 本質上做不到。** 一顆 PVC 無法按 schema / tablespace 拆給不同租戶。

**怎麼讓「不支援」不變成「悄悄壞掉」。** ADR-024 有一條對每個租戶的看門告警 `CustomRecipeDiskInert`：租戶設了磁碟告警、相關 Pod 也在跑，但歸屬後的指標一直沒出現，它就會響。於是 N:1 客戶在遷移當天就會收到「這條告警沒生效」的明確訊號，而不是無聲漏報。等真有 N:1 客戶（例如讀寫分離的資料庫）需要基礎設施告警，再把映射補上。

相關：[ADR-024](024-version-aware-threshold-via-dimensional-label.md)、節點健康告警（#809，也用同一套 1:1 歸屬）、本 epic（#692）。

## 相關決策

- [ADR-005: 投影卷掛載 Rule Pack](./005-projected-volume-for-rule-packs.md) — Recording Rules 透過同一 Projected Volume 機制分發
- [ADR-004: Federation 架構——中央 Exporter 優先](./004-federation-central-exporter-first.md) — Federation 下的映射一致性
- [ADR-001: 嚴重度 Dedup 採用 Inhibit 規則](./001-severity-dedup-via-inhibit.md) — 映射後指標仍適用 inhibit dedup

## 參考資料

- [`docs/architecture-and-design.md`](../architecture-and-design.md) §2.3 — Tenant-Namespace 映射模式
- [`scaffold_tenant.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/scaffold_tenant.py) — 現行 `--namespaces` N:1 支援
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — 路由產生器（ADR-007 相關）
- [Prometheus Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — 官方文件

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [001-severity-dedup-via-inhibit](001-severity-dedup-via-inhibit.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum](002-oci-registry-over-chartmuseum.md) | ⭐ |
| [003-sentinel-alert-pattern](003-sentinel-alert-pattern.md) | ⭐⭐ |
| [004-federation-central-exporter-first](004-federation-central-exporter-first.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs](005-projected-volume-for-rule-packs.md) | ⭐⭐⭐ |
| [006-tenant-mapping-topologies](006-tenant-mapping-topologies.md) | ⭐⭐⭐ |
| [README](README.md) | ⭐⭐⭐ |
| ["架構與設計 — 動態多租戶警報平台技術白皮書"](../architecture-and-design.md) | ⭐⭐⭐ |
| ["架構與設計 — 附錄 A"](../architecture-and-design.md#附錄角色與工具速查) | ⭐⭐ |
