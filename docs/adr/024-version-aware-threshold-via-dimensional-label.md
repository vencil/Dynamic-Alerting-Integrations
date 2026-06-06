---
title: "ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts"
tags: [adr, threshold-exporter, rule-pack, alerting, dimensional-metric, gitops]
audience: [platform-engineers, contributors, sre]
version: v2.8.1
lang: zh
id: ADR-024
tracking_kind: adr
status: accepted
domain: threshold-exporter
created_at: 2026-05-30
updated_at: 2026-06-06
---

# ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts

> **Language / 語言：** **中文 (Current)** | [English](./024-version-aware-threshold-via-dimensional-label.en.md)

## 狀態

✅ **Accepted**（v2.9.0）。本 ADR 記錄一個**宣告式 dimensional 告警引擎**，由兩個共用同一套機械的能力組成：

- **Version-Aware Thresholds**——平台 authored 的 rule pack 上，讓租戶宣告多版本數字閾值。
- **Custom Alerts**——把同一套機械開放給各階層（platform / domain / tenant），用參數化 recipe（非 PromQL）自訂標準 rule pack 涵蓋不到的告警。

兩者均已隨 v2.9.0 落地。Tracker：[#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)（version-aware）、[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741)（custom alerts）；逐 PR 紀錄見 CHANGELOG。

> 本 ADR **不取代、不修改** [config-driven.md §2.6 排程式閾值](../design/config-driven.md)——兩者是並存的不同機制，界線見末節。

## 背景

租戶需要：(1) 規則能事先 commit 進 `conf.d/` 而不立即生效；(2) 生效時機跟 app 升版對齊；(3) 查錯時答得出「現在 Prometheus 跑的是哪個版本」；(4) 不讓 YAML 累積無意義的歷史生效日期。同時，標準 rule pack 涵蓋不到各 domain 與 app 層的告警需求，租戶需要能自訂——但不能因此被迫寫 PromQL。

整套設計受三條既有契約約束：

- **Declarative-only**——平台團隊寫 rule pack 的 PromQL，租戶只在 YAML 設純數字 / 填表單參數。**任何要租戶寫 PromQL 的方案都違反此鐵則**（[ADR-008](008-operator-native-integration-path.md)）。
- **`user_threshold` 已是 dimensional metric**——`user_threshold{tenant, component, metric, severity, <任意 dimensional labels>}` 已支援 `env`、`tablespace_re` 等維度。
- **複雜度集中在 platform team 管理的 rule pack**，不下放租戶；per-tenant cardinality guard 既存（`max_metrics_per_tenant`，超標 truncate）。

## 決策：一個引擎、兩個能力

同一套**宣告式 dimensional 機械**——dimensional-label 模型、scrape-time relabel、rule-pack normalize / 編譯層、graceful-degradation join、promtool 安全網、per-tenant 隔離——撐起兩個能力。Version-Aware 先在平台 rule pack 上證明這套機械能在 prod 安全運轉；Custom Alerts 把它開放給各階層。兩者共用同一個底層引擎、同一個 tenant-api 寫入邊界、同一條 CI pipeline——這正是它們同住一份 ADR 的理由（拆開會切斷「為何地基鋪這麼重」的因果）。

以下七條是構成這套引擎的關鍵決策，每條附 trade-off。

### 1. 用 metric 的 dimensional `version` label 表達多版本閾值（非新 schema）

cutover 是 **emergent behavior**：升版後 app metric 帶上哪個 `version`，PromQL join 就對齊哪個版本的閾值。**既有的 dimensional-label 機制在 exporter parse / emit 零改動下就能產出這個 shape**——租戶今天就能寫：

```yaml
tenants:
  db-a:
    container_cpu{version="v1"}: "80"
    container_cpu{version="v2"}: "60"
```

exporter 直接吐出（無須新程式碼）：

```
user_threshold{tenant="db-a", component="container", metric="cpu", severity="warning", version="v1"} 80
user_threshold{tenant="db-a", component="container", metric="cpu", severity="warning", version="v2"} 60
```

`version` 與 `env` / `tablespace_re` 是同一條 dimensional 路徑、**單一心智模型**。*Trade-off*：選此而非新增 `versioned:` YAML block，是 **reuse-over-build**——避開觸碰千租戶 hot-reload 的 config parser（最高 blast radius），代價是多版本散在多個相鄰 key（review 較弱但單一 diff hunk）。「零改動」只指**閾值宣告半邊**；真正的工程量在下方的 normalize layer + metric-side 的 version 注入。

### 2. Rule-pack normalize layer：version 注入 → 降級 fallback → per-severity 拆

這是 version-aware 真正的工程核心。app metric（如 cAdvisor 的 `container_cpu_usage_seconds_total`）**不帶 version**，須在 recording rule 把 `app.kubernetes.io/version`（經 kube-state-metrics `kube_pod_labels` relabel）注入為 `version` label，且每一層 `by(...)` 聚合都要保留它。normalize 後兩邊用 `label_replace(..., "version", "default", "version", "^$")` 把缺漏的 version 補成 `default`，再做 join。

告警規則採**精確-or-降級**結構：

```promql
- alert: PodContainerHighCPUWarning
  expr: |
    (
      # 精確命中該 (tenant, version) 的閾值（one-to-one）
      app_metric_vlabeled
      > on(tenant, version) group_left()
        threshold_vlabeled{severity="warning"}
    )
    or
    (
      # 降級：無對應版號閾值 → 套 version="default"。group_left 保留 metric 真實版號
      (app_metric_vlabeled unless on(tenant, version) threshold_vlabeled{severity="warning"})
      > on(tenant) group_left()
        threshold_vlabeled{version="default", severity="warning"}
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
    * on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
  labels:
    severity: warning   # 固定於 alert label；Critical 規則鏡像複製
```

三個刻意設計：

- **動態降級**——租戶只在「特定大版號要特殊閾值」時才宣告 `{version="v2"}`，日常小改版（每天多次 deploy）不需同步改告警 YAML，缺對應版號時自動沿用 `default`、不丟 series。這把「observed-but-not-declared = silent gap」這個最高風險從「事後補救」升級為「架構內建」。
- **拆 per-severity 規則**——**不可**用 `group_left(severity)`：`version × severity` 維度交織會在 join 形成 cardinality 死鎖（精確分支 one-to-many 崩、fallback 分支 many-to-many 崩）→ 整個告警引擎癱瘓。固定 severity 後 RHS 退化為 singleton，所有 join 變乾淨的 one-to-one / many-to-one。
- **非對稱 join key 是安全的**——閾值比較用 `on(tenant, version)`，但 `user_state_filter`（維護）與 `tenant_metadata_info` 是不帶 version 的 per-tenant singleton，`unless on(tenant)` / `group_left` 方向合法、不被 version 破壞。

⛔ **部署前提（HARD）**：kube-state-metrics 必須設 `--metric-labels-allowlist=pods=[app.kubernetes.io/version]`，否則 `kube_pod_labels` 不帶 version、注入 join 匹配空集、版本閾值**靜默 inert**。三層防禦守此前提：runtime sentinel（`VersionAwareThresholdInert`）+ CI static lint（`check_ksm_version_allowlist.py`）+ operator manifest allowlist。在 metric-side 注入上線前，整套機制 inert 且 100% 向下相容（缺 version → 補 default → 對齊未版本化閾值）。

*範例 metric*：v2.9.0 pilot 對 `container_cpu` + `container_memory` 兩 metric 落地，作為「能力證明」；其餘 pack 的 version-awareness 列 defer-with-trigger（客戶對非-k8s metric 提出版本需求時）。

### 3. Custom Alerts：平台 authored 的參數化 recipe，永不寫 PromQL

「永不寫 PromQL」這條鐵則擋的三個風險中最難的「**跨租戶隔離**」已被既有架構結構性解掉（scrape job 從 namespace 烙 `tenant` label、租戶偽造不了）。故真正的設計問題不是「能不能做」，而是「**表達力 + 規則數成本**」。

- **MVP = 參數化 recipe**：平台 authored 的 recipe 庫（**6 個**：threshold / rate / ratio / absence / p99_latency / forecast），各階層填表（metric / window / op / 閾值 / severity / `mode`）→ 存成 declarative YAML。Level 2 bounded-DSL / Level 3 raw-PromQL 逃生門列 Future。*Trade-off*：表達力被 recipe 庫框住，換來安全是**結構性的**（valid spec → valid PromQL，不靠運氣）。
- **階層式 scope = 宣告層級**：recipe 宣告在 `_defaults.yaml` 哪一層（平台 / domain / subdomain / tenant leaf，見 [ADR-017](017-defaults-yaml-inheritance-dual-hash.md)）就決定其 blast radius。Domain SRE 寫一次、套整棵子樹。**平台 / domain 政策 = 生成規則、租戶不可 override**——規則住在租戶 RBAC 寫不到的 `_defaults.yaml` + CI 生成檔，是**結構性**不可覆寫而非靠 lock 標記。
- **Metric discovery = 唯讀的無狀態 Prometheus proxy**：tenant-api 向 Prometheus 查**該租戶**自身的 app-metric 名稱（`{tenant="<authID>"}` 篩），給 portal 當 recipe 的 metric 自動完成來源——不自建會與 Prometheus 漂移的 catalog 狀態。查詢字串強制鎖定（租戶輸入觸不到 matcher 結構、`tenant` label scrape 端 branded、前綴搜尋 escaped），並用既有 per-caller rate limiter 防代理驚群。租戶為「將來會出現」的 metric 預建 recipe 是合法的（GitOps 真空期），故後端**不**硬擋「metric 當下不存在」，改由 portal 軟警告。
- **Recipe authoring UX = 智慧表單、後端擁有寫入**：portal 的 recipe builder 是純 `(Context) => RecipeObject` 元件、**不擁有寫入**；寫入經 tenant-api 的 `PUT .../custom-alerts`，**後端獨佔 YAML round-trip**（client 只送 / 收 JSON）。寫入用 yaml.Node AST surgery 保留註解 / 縮排，並以整檔 hash 做樂觀並發控制（base_hash 不符 → 409）。

### 4. 向量化編譯：成本誠實 + 三件護欄

編譯器把 recipe + 參數生成**向量化 `group_left` 規則**：一條 `app_metric > on(tenant[,version]) group_left(...) <該 recipe 的 user_threshold>` 涵蓋所有宣告該 recipe 的租戶——**規則數 = recipe 形狀數（shape），非租戶數**。

**效能誠實**：O(M)-與-N-無關**只對「共享指標」成立**。向量化消掉「同指標扇出複製 N 條」，但**消不掉「不同 metric 必然不同規則」**——租戶 A 的 `order_created_total` 與 B 的 `payment_failed_total` 必生成兩條。故 custom-alert 規則數隨**自訂告警總數線性增長**，不享 rule pack 對 [benchmarks.md §2](../benchmarks.md) 的 O(M) 保證。**護欄三件組**：(a) 硬性 `max_custom_recipes` per-tenant cap 封頂；(b) 全域 rule-count budget（cap 值由實測 rule-eval-duration 反推）；(c) 壞 rule 只炸自己的規則檔 group + Prometheus 原生 rule group `limit` + promtool hard gate。

**部署源 = live conf.d**：編譯源必須是 exporter 實際服務的同一份 conf.d，否則生成規則的 shape 對不上 exporter emit 的 `user_threshold` series → 規則永不 fire（靜默失效）。custom-alert pack 是**租戶自訂、非平台覆蓋**，故從平台的 rule-pack / alert count 統計排除（badge 不變）。

### 5. 驗證雙層：Go in-process preflight + CI 權威（prod image 不打包 promtool）

- **Layer 1 — tenant-api 的 Go preflight**（in-process、快、stateless per-tenant 輸入閘）：`PUT` 當下就驗租戶 recipe spec、無效回 HTTP 400 + `Violations[]`，**壞輸入不進 repo**。複用與 exporter **共用同一份** Go 驗證器（metric regex / reserved label / recipe·op·severity·mode·for·horizon enum / ratio-floor ∈(0,1) / NaN·Inf）。
- **Layer 2 — CI Python compiler**（全域權威，stateful + promtool）：跨樹 / 階層繼承 / 向量化 / 模板 promtool——唯一握有全域 SOT 的權威 gate。

*Trade-off*：**prod image 不打包 promtool / Python**。租戶從不寫 PromQL、recipe 模板平台 authored，故 valid spec → valid PromQL；promtool 只多抓「模板 regression」= 平台問題，留 **CI golden** 守，不放進請求路徑（避免 Go 服務背 Python runtime + 熱路徑 subprocess 的 image 膨脹與 dev-prod 邊界模糊）。「own recipe 重複繼承政策」這類需要全域樹的檢查 defer 至 CI——tenant-api 本地磁碟在 GitOps 真空期不是全域 SOT，熱路徑 tree-walk 會用過期樹 false-pass。

### 6. Silent 與 routing 隔離：複用既有 sentinel + inhibit

custom-alert 的 `mode`（page / silent）label 經 `group_left` 一路帶到 alert。Alertmanager 端的消費：

- **Silent 走 sentinel + inhibit，不走 route-to-null**——這是刻意複用 [ADR-003](003-sentinel-alert-pattern.md) 既有的三態 silent 典範。編譯器注入一條全域 sentinel `CustomRecipeSilent`（severity=none，從 `user_threshold{mode="silent"}` 導出），Alertmanager inhibit 以 `equal:[tenant, name]` 只抑制該 (租戶, recipe) 的通知；靜音的告警仍是 Prometheus 的 `ALERTS{...}` series → **silent ≡ dashboard-only**。選 inhibit 而非 route-to-null：與平台一致、AM UI 可見抑制狀態、且 dict-keyed 的 route-to-null 在繼承上會破壞語意（見否決）。
- **routing 隔離**：custom 告警帶靜態 `component="custom"` label（平台告警零此 label，精確 match 無歧義）；Alertmanager route **居首 + `continue:false`** 截在平台 NOC route 之前，避免租戶告警風暴灌進平台 NOC。`group_by` 用 `[tenant, alertname]`（alertname 已含 shape，不揉成一團）。page-mode 的 firehose receiver MVP 為隔離的空 receiver（outbound 接 log backend 而非限流的 IM，避 429→queue→OOM）。

### 7. Forecast recipe：趨勢 / 耗盡預測（雙模式）

`forecast` 是第 6 個 recipe，回應「磁碟 / 記憶體照趨勢未來 N 小時內會耗盡」這類真實需求。raw `predict_linear` 是公認的 false-positive 製造機（瞬時尖峰被線性外推），故由平台把 FP 來源堵死、封裝成參數化 recipe——這正是 recipe 模式相對「租戶自寫 PromQL」的核心價值。

- **單一 recipe、雙模式**：有 `capacity_metric` → 比例 mode（預測比例掉破 floor ∈(0,1)）；無 → 原始值 mode（預測 gauge 穿越絕對門檻）。
- **lookback 不給租戶填**，平台推導 `lookback = max(2·horizon, 1h)`（整數秒）；租戶只填 `horizon`（enum，cardinality 鎖定）。理由：lookback 是專家旋鈕、暴露即最大 foot-gun，且 `horizon ≤ lookback` 結構恆成立、免額外驗證。
- **cold-start 資料量 gate**（`count_over_time(base[lookback]) > N`）擋剛部署時樣本太少的亂跳；**gauge-only**（counter 須先 rate）。

具體例（租戶宣告，不碰 PromQL）：

```yaml
- recipe: forecast
  name: disk_will_fill
  metric: kubelet_volume_stats_available_bytes
  capacity_metric: kubelet_volume_stats_capacity_bytes
  op: "<"
  horizon: 4h
  threshold: "0.15:warning"   # 預測 4h 內可用比例掉破 15%
```

設計 forecast 時順帶修一條既有 recipe 就帶的正確性 bug：`for`（sustain 時長）原本不在 recipe 的 shape 身分內，兩租戶共用 shape 但 `for` 不同時，後者的 `for` 被靜默丟棄。`mode` 能用 `group_left` 搭資料平面，但 `for` 是控制平面的靜態規則屬性救不了——故把 `for` 納入 shape slug + schema enum（含並保留既有 `default: "1m"`），把 cardinality 鎖成常數。

## 資料流：Ingest → Define → Compile

- **Ingest**——租戶 app metric 進 Prometheus + scrape-time 烙 `tenant` / `version`。複用既有 `tenant-exporters` job + 平台預設 relabel 把 `app.kubernetes.io/version` → `version`（集中式，不下放 per-tenant）。
- **Define**——各階層在 `_defaults.yaml` 填 recipe 表單，存成 declarative YAML；宣告層級決定 scope。
- **Compile**——recipe → 向量化 `group_left` 規則 → version graceful-join → promtool gate → 規則檔隔離 → GitOps 部署（operator manifest + ConfigMap projected volume + Prometheus reload；無 ArgoCD/Flux）。

## 復用既有機制（為何兩能力同住一份 ADR）

| Custom Alerts 需要 | 復用的既有資產 |
|---|---|
| scrape ingestion + version 烙印 | `tenant-exporters` job + 平台預設 `app.kubernetes.io/version` relabel |
| version graceful join | version-aware normalize layer 的 `version=~"\|default"` 左外連接 |
| 跨租戶隔離 | namespace→`tenant` scrape-stamp + prom-label-proxy（[ADR-020](020-tenant-federation.md)） |
| 階層 scope | `_defaults.yaml` 目錄樹繼承（[ADR-017](017-defaults-yaml-inheritance-dual-hash.md)） |
| 向量化 1-rule-蓋全租戶 | rule pack 既有 `on(tenant) group_left` O(M) pattern |
| silent | [ADR-003](003-sentinel-alert-pattern.md) sentinel + inhibit |
| 寫入驗證 / 預設融合 / 維護抑制 | tenant-api `validate()` + `MergeTenantWithRootDefaults` + `user_state_filter{filter="maintenance"}` |

唯一 **net-new 核心**：recipe 庫 + 參數 schema + recipe 編譯器 + discovery proxy + cost cap + recipe 編輯 UX。

## 關鍵 Trade-off

核心判斷是 **reuse-over-build**：目標能力 90% 已存在於既有 dimensional 機制。額外打造新 schema 的唯一實質增益是 authoring 分組，但代價是觸碰 hot-reload critical path + 引入功能重複的第二條 default-注入路徑 + 把向下相容從「自動成立」變成「需驗證」。因此一律以最小 net-new surface 復用既有機制，把成本誠實標出（custom-alert 規則數對 unique metric 是線性，用 cap 封頂），換來各階層可自訂業務告警而不寫 PromQL。

## Consequences

**變容易**：升版閾值 cutover 與 K8s rolling update 自動對齊、無時序漂移；「現在跑什麼版本」可由 `count by(version)(<app metric>)` 直接答出；YAML 不再累積歷史生效日；各階層自訂告警不需平台逐一加 rule。

**變難 / 新增的 failure mode**（及其化解）：

- **observed-but-not-declared**（metric 在跑、沒宣告該版號閾值）→ 動態降級**架構性消解**（自動 fallback 到 default、不丟 series）。殘餘：typo 版號靜默落 default，靠 orphan 偵測抓。
- **`default` 命名碰撞**——da-guard **禁止顯式 `default`**（保留給 fallback），避免與未版本化閾值在同 bucket 取 max 時的歧義。
- **cardinality 截斷必須確定性**——per-tenant guard 截斷前須對 dimensional keys 確定性排序（無版號 / `default` 優先保護），否則 Go map 隨機序會讓被截的版號隨 scrape 忽隱忽現 → **alert flapping + 重複 page**。
- **共享 `user_threshold` 跨 pack 洩漏**——非 pilot pack 的 normalize matcher 加 `version=~"|default"`（只收無版號 / default），CI guard 失效時仍保既有告警安全（防 double-count）。
- **Dashboards / Portal query 假設無 version label**——租戶開始寫 version key 後，未聚合 query 會多出帶 version 的 series → 須審 Grafana / portal panel。
- **ops-review 工具的繼承一致性**——`_custom_alerts` 的繼承是 UNION（own + inherited），與通用陣列的 REPLACE 不同；診斷工具（describe_tenant / blast_radius）須委派編譯器的繼承解析（SSOT），否則會對 override 租戶漏報平台政策變更。

**關鍵不變式**（驗收契約，非 tracking checklist）：(1) 既有未寫 version 的租戶升級後行為 100% 等價（無 series 數變動）；(2) over-cap 截斷跨多次 scrape 截掉的是固定版號（無 flapping）；(3) `mode: silent` recipe 觸發時不進 PagerDuty / Slack、只留 Grafana 痕跡；(4) rolling update 結束、舊版 metric 消失後，正在燒的告警能正常收到 Resolve（不留殭屍告警）；(5) version-aware metric 上的 recipe 自動 graceful-join、label 缺時落 default、不產 NaN / 空集。

## 否決的替代方案

- **`ScheduledValue.from/until` 絕對日期 schema 擴展** — YAML 累積無意義生效日（過期 `from` 永遠 true）+ 雙寫 atomicity 風險。把時間軸吞進 declarative config 是結構性錯誤。
- **`POST /active-version` 寫狀態 API** — 引入第二個 state 破壞 single SOT；rolling 中段呼叫造成 transient 不對齊。Metric 帶 version 流進來就是 SOT。
- **Scheduled PR Merge orchestration** — Git 瞬時二元 merge 無法 align K8s 5–10 分鐘漸進發佈 + GitOps 傳遞鏈延遲 + helm rollback 不反向 revert Git PR。
- **PromQL normalize 用 `or on() vector(0)` 補假值** — 破壞下游聚合、對「值為 0 即告警」rule 製造 false positive。正解 = `label_replace(..., "version", "default", "version", "^$")`。
- **租戶直接寫 PromQL** — 違反 declarative-only 鐵則；概念正確但被 adapt 成 dimensional-label / recipe 方案。
- **Silent 走 route-to-null receiver** — 與 [ADR-003](003-sentinel-alert-pattern.md) 既有 inhibit 典範不一致、AM UI 看不出抑制狀態；改複用 sentinel + inhibit。
- **把 `_custom_alerts` 從 list 改 dict（以名為 key）** — dict-merge 是 override-on-key-collision，**不等於** ADR-024 要的 UNION 繼承（租戶重用平台 recipe 名即可悄悄抹掉政策）；且 list 形態貫穿整個引擎（Go exporter / tenant-api / portal / 編譯器）→ blast radius 不成比例。ops-review 工具改委派編譯器繼承解析即可，不動 schema。

## 與排程式閾值（§2.6）的界線

[config-driven.md §2.6](../design/config-driven.md) 的 `ScheduledValue.overrides: [{window, value}]` 是 **recurring 時間窗口**機制，與本 ADR 刻意分開、可同時作用於同一租戶：

| 維度 | §2.6 排程式閾值 | ADR-024 Version-Aware |
|---|---|---|
| 切換軸 | **時間**（recurring 窗口，看 wall-clock） | **狀態 / version label**（不評估時間） |
| 觸發來源 | UTC 時鐘到點，每日重複 | app 升版後 metric 帶新 version，one-time cutover |
| 典型場景 | 「夜間 22:00–06:00 放寬到 200」 | 「v2 上線後 CPU 閾值由 80 收緊到 60」 |

兩者正交：§2.6 處理週期性時段，本 ADR 處理一次性的版本對齊。

## Cross-Reference

- [ADR-003: Sentinel Alert 模式](003-sentinel-alert-pattern.md) — silent / 三態的 sentinel + inhibit 基礎。
- [ADR-008: Operator-Native 整合路徑](008-operator-native-integration-path.md) — declarative-only 鐵則。
- [ADR-017: `_defaults.yaml` 繼承](017-defaults-yaml-inheritance-dual-hash.md) — 階層 scope 的目錄樹繼承。
- [ADR-020: Tenant Federation](020-tenant-federation.md) — 跨租戶讀路徑隔離（prom-label-proxy）。
- [Version-Aware Thresholds 使用攻略](../scenarios/version-aware-thresholds.md) — 租戶宣告 + 平台 KSM 設定的操作面。
- [config-driven.md §2.x](../design/config-driven.md) — dimensional 閾值與 recipe 的活體規格。
