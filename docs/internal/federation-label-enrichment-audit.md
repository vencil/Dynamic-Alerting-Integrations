---
title: "Federation Data-Layer Label Enrichment Audit（IV-2.0 / #505）"
tags: [internal, federation, audit]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# Federation Data-Layer Label Enrichment Audit

> ADR-020 IV-2.0（issue [#505](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/505)）的前置 audit。在 federation MVP（IV-2e policy schema / IV-2g smoke test）開工前，盤點 data-layer 的租戶 label 現況，確認 prom-label-proxy 注入的 matcher 配得到 series。

## 為什麼做這個 audit

ADR-020 §前提約束 把「data-layer label enrichment」列為 IV-2 的 blocker：Layer-3 的 prom-label-proxy 對每個 PromQL selector 強制注入一個 per-tenant label matcher。若某 metric 原生不帶該 label，注入後的查詢得到 **empty vector** —— 租戶看到 dashboard 空白、報修「federation 壞了」，SRE 要從 token 一路查到 scrape config 才找得到根因。是典型的 silent-failure 地雷。

本 audit 在開工前盤點哪些 metric family 已帶租戶 label、哪些沒有，產出 federation whitelist 的 eligible / ineligible 初始清單（IV-2e #510 的輸入）。

## 核心發現：label 名是 `tenant`，不是 `tenant_id`

audit 最重要的發現不是「某些 metric 缺 label」，而是 **label 的名字本身在 data layer 與 federation stack 之間不一致**：

| 層 | 租戶 label 名 | 證據 |
|---|---|---|
| Data layer（實際） | **`tenant`** | `k8s/03-monitoring/configmap-prometheus.yaml` job `tenant-exporters` relabel `target_label: tenant`；`components/threshold-exporter/app/collector.go` `labelNames := []string{"tenant", ...}`；`k8s/03-monitoring/configmap-rules-*.yaml` 中 DB / middleware 類 rule pack（13/15）一律 `on(tenant)` join / `by(tenant)` 聚合（其餘 2 個 `operational` / `platform` 為不分租戶的平台內部規則，亦不用 `tenant_id`） |
| Federation proxy（IV-2a #506 已 merge） | `tenant_id` | `helm/federation-proxy/values.yaml` `tenant.label: tenant_id` → `prom-label-proxy -label=tenant_id` |
| ADR-020 prose | `tenant_id` | §前提約束 / 四層路由圖 / §實作計畫 多處寫 `{tenant_id="<X>"}` |

`tenant_id` 在整個 `k8s/` data-layer 設定裡 **一次都沒出現**。

**後果**：prom-label-proxy 以 `-label=tenant_id` 啟動，對每個查詢注入 `{tenant_id="<X>"}`。後端 metric 全部帶的是 `{tenant="<X>"}` → 注入後配不到任何 series → **每一個 federated 租戶查詢回 empty vector**。這不是某些 metric family 的局部問題，是 label 名字全錯、範圍 100%。ADR-020 把「data layer 已帶 `tenant_id`」當 prerequisite，但此前提從未成立 —— #505 audit 的存在正是為了在開工前攔下它。

### 決議

Federation **對齊平台既有的 `tenant`**。反方向（把平台的 relabel config + threshold-exporter Go code + 13 個 tenant-scoped rule pack + 整合文件全部改名 `tenant_id`）是大規模 breaking change，排除。

最小且充分的修正點是 `prom-label-proxy` 的 `-label` flag —— 它是「注入到 metric 的 label 名」的唯一決定點。JWT claim 名為 `tenant_id`、gateway 的 `x-tenant-id` header 名 **不需要改**：claim / header 名與 metric label 名是獨立命名空間，proxy 取 claim 的「值」、用 `-label` 指定的「名字」注入。

issue #505 的 PR 一併處理：

- `helm/federation-proxy/values.yaml`：`tenant.label` `tenant_id` → `tenant`（+ chart README / chart version）。
- `docs/adr/020-tenant-federation.md` §前提約束：label 名對齊 `tenant` + 本 audit 的修正註記。

## 現況盤點表

| Metric family | 來源 | 帶 `tenant` label？ | 機制 / 現況 |
|---|---|---|---|
| `mysql_*` / `pg_*` / `redis_*` / `mongodb_*` / `elasticsearch_*` / `oracle_*` / `db2_*` / `clickhouse_*` / `kafka_*` / `rabbitmq_*` / `nginx_*` / JVM 等 DB / middleware exporter metric | 各 exporter，經 Prometheus job `tenant-exporters` 抓取 | ✅ 有 | job `tenant-exporters` 的 `relabel_configs` 從 K8s namespace 推導 `target_label: tenant` |
| `user_threshold` / `user_state_filter` / `user_silent_mode` / `user_severity_dedup` / `da_config_event` / `tenant_metadata_info` | platform threshold-exporter | ✅ 有 | exporter 原生 emit `tenant` label（`collector.go`） |
| `tenant:*`（recording-rule 輸出，如 `tenant:mysql_threads_running:avg1m`、`tenant:container_cpu_percent:by_container`） | rule pack recording rules | ✅ 有 | recording rule `sum by(tenant)(...)` / `label_replace` 產出 |
| `container_*`（cAdvisor） | kubelet cAdvisor，Prometheus job `kubelet-cadvisor` | ❌ **無** | job 只有 namespace filter（`regex: db-.+`），**無 `target_label: tenant` relabel**。租戶維度目前靠 recording rule 後補 `label_replace(namespace)` |
| `kube_*`（kube-state-metrics） | kube-state-metrics，job `monitoring-components` | ❌ **無** | 自 `monitoring` namespace 抓取，平台內部用途，未打租戶 label |
| `node_*`（node-exporter） | — | ❌ **未抓取** | 現行 Prometheus 設定無 node-exporter job |

## Federation whitelist — eligible / ineligible 初始清單

這份清單是 IV-2e（#510）policy schema 的 platform whitelist 輸入。

### ✅ Eligible（可直接列入 whitelist）

- 所有 DB / middleware exporter metric（`mysql_*` / `pg_*` / `redis_*` / …）—— 經 `tenant-exporters` job 保證帶 `tenant`。
- threshold-exporter 的 `user_*` / `da_config_event` / `tenant_metadata_info`。
- `tenant:*` recording-rule 衍生 metric —— 含 `tenant:container_cpu_percent:*` 等容器類派生指標。

### ⛔ Ineligible（不可直接列入，需先補救）

- **`container_*`（raw cAdvisor）**：原生無 `tenant`。注意：衍生的 `tenant:container_*:` recording-rule metric **是** eligible —— 租戶若要容器 CPU / 記憶體，whitelist 應收 recording-rule 形式，不收 raw cAdvisor。
- **`kube_*`（kube-state-metrics）**：原生無 `tenant`，且自平台 namespace 抓取。
- **`node_*`**：未抓取。

## 範圍界線（本 audit 不涵蓋）

- **Label 的「值」對應**：本 audit 確認 label 的**名字**（`tenant`）以及各 metric family 是否帶它。proxy 注入的 `{tenant="<v>"}` 中 `<v>` 來自 federation token 的 `tenant_id` claim 值，必須等於 data-layer `tenant` label 的值（job `tenant-exporters` 由 K8s namespace 推導）。「token 的租戶識別值 == namespace == `tenant` label 值」這個 value-contract **不在本 audit 範圍** —— 標籤名修對是必要、非充分條件，value-contract 由 IV-2j 端到端整合測試（#516）驗證。
- **VictoriaMetrics cluster 路徑**：ADR-020 的 vm-cluster federation 走 gateway URL-rewrite 到 `/select/<accountID>/`（accountID 路由，非 label injection）—— label enrichment 對該路徑不適用。本盤點僅涵蓋 prom-label-proxy（front Prometheus / Thanos / VictoriaMetrics 單機）路徑。

## Follow-up

各缺 label 項目的後續處理（本 audit 不在 #505 內修）：

1. **cAdvisor `container_*` scrape-time relabel**（建議，非 #505 scope）：在 `kubelet-cadvisor` job 加 `metric_relabel_configs` 從 namespace 推導 `tenant`，比照 `tenant-exporters` job。在此之前，raw `container_*` 排除於 whitelist，容器類查詢走 `tenant:container_*:` recording rule。
2. **kube-state-metrics / node-exporter**：目前非 federation 範圍。若未來租戶要求 k8s 物件層指標，需先設計其租戶 label 注入。
3. **IV-2e admission validator（#510）**：validator 對「過去 24h 該 metric 至少一筆帶 `tenant` label 的 sample」做檢查 —— 把本盤點表的 ineligible 清單變成 mechanical gate，而非靠人工記得。validator 檢查的 label 名為 `tenant`（對齊本 audit 的決議，非 ADR 原稿的 `tenant_id`）。
4. **整合文件**：`docs/integration/byo-prometheus-integration.md` 已教客戶注入 `tenant`（正確），但未明寫「漏注入 = federation 查詢靜默空白」的 silent-failure 警告 —— 建議 IV-2h（#513）文件補一段 callout。

---

_2026-05-17 — ADR-020 IV-2.0 前置 audit。盤點依據：`k8s/03-monitoring/configmap-prometheus.yaml`、`k8s/03-monitoring/configmap-rules-*.yaml`、`components/threshold-exporter/app/collector.go`、`helm/federation-proxy/`。_
