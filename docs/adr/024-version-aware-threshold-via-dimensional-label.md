---
title: "ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts"
tags: [adr, threshold-exporter, rule-pack, alerting, dimensional-metric, gitops]
audience: [platform-engineers, contributors, sre]
version: v2.9.0
lang: zh
id: ADR-024
tracking_kind: adr
status: accepted
domain: threshold-exporter
created_at: 2026-05-30
updated_at: 2026-10-23
---

# ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts

> **Language / 語言：** **中文 (Current)** | [English](./024-version-aware-threshold-via-dimensional-label.en.md)

## 狀態

✅ **Accepted**（v2.9.0）。本 ADR 記錄一個**宣告式 dimensional 告警引擎**，由兩個共用同一套機械的能力組成：

- **Version-Aware Thresholds**——平台 authored 的 rule pack 上，讓租戶宣告多版本數字閾值。
- **Custom Alerts**——把同一套機械開放給平台與租戶，用參數化 recipe（非 PromQL）自訂標準 rule pack 涵蓋不到的告警。

兩者均已隨 v2.9.0 落地。Tracker：[#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423)（version-aware）、[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741)（custom alerts）；逐 PR 紀錄見 CHANGELOG。

📝 **修訂（2026-10-16）**：自訂告警的宣告位置以 exporter 實際送出的為準，不做 `_defaults.yaml` 子樹繼承；一組租戶共用的政策改寫在 profile（見 §3「宣告位置」）。決策與實測見 [#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)。

📝 **修訂（2026-10-17）**：自訂告警編譯器改照 exporter 送出的編譯（見 §3「宣告位置」）；每租戶上限改由 tenant-api 寫入檢查、da-guard 與 validate-config 擋（見 §4）。

📝 **修訂（2026-10-21）**：自訂告警的逐條判定、recipe_id 與規則產生只有一份 Go 實作，recipe 的列舉、格式與數值界限寫在一份契約檔，其他各處由它產生（見 §3、§5、§9）；`describe_tenant` 只顯示 exporter 送出的清單，「這次變更影響什麼」只由 config-diff 回答（見「後果」）。實作依 [#3229](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3229) 分步進行；編譯器與 recipe-preview 已改用 Go（2026-10-23），`describe_tenant` 的改動還沒做。

📎 **Addendum** (v2.10.0) — 磁碟與 PVC 告警（容量逐顆 PVC 評估 + 需 CSI driver + 兩道防線），見下方 §Addendum；epic [#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692)。

> 本 ADR **不取代、不修改** [config-driven.md §2.6 排程式閾值](../design/config-driven.md)——兩者是並存的不同機制，界線見末節。

## 背景

租戶需要：(1) 規則能事先 commit 進 `conf.d/` 而不立即生效；(2) 生效時機跟 app 升版對齊；(3) 查錯時答得出「現在 Prometheus 跑的是哪個版本」；(4) 不讓 YAML 累積無意義的歷史生效日期。同時，標準 rule pack 涵蓋不到各 domain 與 app 層的告警需求，租戶需要能自訂——但不能因此被迫寫 PromQL。

整套設計受三條既有契約約束：

- **Declarative-only**——平台團隊寫 rule pack 的 PromQL，租戶只在 YAML 設純數字 / 填表單參數。**任何要租戶寫 PromQL 的方案都違反此鐵則**（[ADR-008](008-operator-native-integration-path.md)）。
- **`user_threshold` 已是 dimensional metric**——`user_threshold{tenant, component, metric, severity, <任意 dimensional labels>}` 已支援 `env`、`tablespace_re` 等維度。
- **複雜度集中在 platform team 管理的 rule pack**，不下放租戶；per-tenant cardinality guard 既存（`max_metrics_per_tenant`，超標 truncate）。

## 決策：一個引擎、兩個能力

同一套**宣告式 dimensional 機械**——dimensional-label 模型、scrape-time relabel、rule-pack normalize / 編譯層、graceful-degradation join、promtool 安全網、per-tenant 隔離——撐起兩個能力。Version-Aware 先在平台 rule pack 上證明這套機械能在 prod 安全運轉；Custom Alerts 把它開放給平台與租戶。兩者共用同一個底層引擎、同一個 tenant-api 寫入邊界、同一條 CI pipeline——這正是它們同住一份 ADR 的理由（拆開會切斷「為何地基鋪這麼重」的因果）。

以下九條是構成這套引擎的關鍵決策，每條附 trade-off。

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

- **MVP = 參數化 recipe**：平台 authored 的 recipe 庫（**7 個**：threshold / rate / ratio / absence / p99_latency / forecast / slo_burn_rate；最後一個見 [ADR-031](031-slo-burn-rate-recipe.md)），平台或租戶填表（metric / window / op / 閾值 / severity / `mode`）→ 存成 declarative YAML。Level 2 bounded-DSL / Level 3 raw-PromQL 逃生門列 Future。*Trade-off*：表達力被 recipe 庫框住，換來安全是**結構性的**（valid spec → valid PromQL，不靠運氣）。
- **宣告位置 = 每個租戶一個來源**：exporter 為每個租戶只從一處取 `_custom_alerts`，依序是租戶自己的條目、根目錄平台檔（例如 `_platform.yaml`、根 `_defaults.yaml`）裡該租戶的 `tenants:` 條目、租戶綁定的 profile；取到第一處就停，**不合併**。任何一層 `_defaults.yaml` 頂層或 `defaults:` 底下的 `_custom_alerts` 都不會送出——**不做子樹繼承**；根目錄平台檔頂層的也不送。子目錄 `_defaults.yaml` 裡的（頂層或 `defaults:` 底下），`da-guard effective` 對沒有其他來源的租戶標 `not_served`（`value_rejected`）；根目錄 `_defaults.yaml` 與平台檔頂層的不列出；寫在根 `_defaults.yaml` 的 `defaults:` 底下則整份檔解碼失敗（da-guard exit 3），檔裡其他預設值也一併不送出。要讓一組租戶共用同一條政策，就寫在 profile、讓這些租戶綁定它。這是**預設，不是不可覆寫的政策**：租戶寫了自己的 `_custom_alerts` 就整個取代 profile 的清單，也能改綁或解除綁定。

  範例（`finance/` 底下兩個租戶綁定同一個 profile，`pay-b` 另寫了自己的 recipe）：

  ```yaml
  # _profiles.yaml
  profiles:
    finance:
      _custom_alerts:
        - {recipe: ratio, name: payment_failure_ratio, metric: payment_failed_total,
           denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.01:critical"}
  ```

  ```yaml
  # finance/_defaults.yaml——不會送出
  _custom_alerts:
    - {recipe: ratio, name: refund_ratio, metric: refund_total,
       denominator_metric: payment_attempts_total, op: ">", window: 5m, threshold: "0.05:warning"}
  ```

  ```yaml
  # finance/pay-a.yaml
  tenants:
    pay-a: {_profile: finance}
  ```

  ```yaml
  # finance/pay-b.yaml
  tenants:
    pay-b:
      _profile: finance
      _custom_alerts:
        - {recipe: threshold, name: pending_settlements_high, metric: pending_settlements,
           op: ">", window: 5m, threshold: "500:critical"}
  ```

  `da-guard served-values` 送出的自訂告警：`pay-a` 是 `payment_failure_ratio`，`pay-b` 是 `pending_settlements_high`；`refund_ratio` 兩個租戶都沒有。

  自訂告警編譯器編的就是這份讀法：編譯改由 `da-guard compile-custom-alerts` 進行，它取 exporter 為每個租戶取得的 `custom_alerts`（與 `da-guard served-values` 是同一段 Go 程式），為送出的與停用的條目產生規則，不另外走 conf.d。上面的範例編出 `payment_failure_ratio` 與 `pending_settlements_high` 兩條規則，`refund_ratio` 沒有規則。條目是否有效、是否與前面某條同 shape 同 severity、它的 recipe_id 與 PromQL，都由這同一份 Go 程式決定（§9），不會有「exporter 送出了門檻、編譯器卻算出別的 recipe_id 或不收」這種門檻沒有規則讀、永遠不告警的條目。`compile_custom_alerts.py` 只負責讀寫檔案、限制 `--out` 寫在指定目錄內、`--check` 比對與輸出序列化。
- **Metric discovery = 唯讀的無狀態 Prometheus proxy**：tenant-api 向 Prometheus 查**該租戶**自身的 app-metric 名稱（`{tenant="<authID>"}` 篩），給 portal 當 recipe 的 metric 自動完成來源——不自建會與 Prometheus 漂移的 catalog 狀態。查詢字串強制鎖定（租戶輸入觸不到 matcher 結構、`tenant` label scrape 端 branded、前綴搜尋 escaped），並用既有 per-caller rate limiter 防代理驚群。租戶為「將來會出現」的 metric 預建 recipe 是合法的（GitOps 真空期），故後端**不**硬擋「metric 當下不存在」，改由 portal 軟警告。
- **Recipe authoring UX = 智慧表單、後端擁有寫入**：portal 的 recipe builder 是純 `(Context) => RecipeObject` 元件、**不擁有寫入**；寫入經 tenant-api 的 `PUT .../custom-alerts`，**後端獨佔 YAML round-trip**（client 只送 / 收 JSON）。寫入用 yaml.Node AST surgery 保留註解 / 縮排，並以整檔 hash 做樂觀並發控制（base_hash 不符 → 409）。

### 4. 向量化編譯：成本誠實 + 三件護欄

編譯器把 recipe + 參數生成**向量化 `group_left` 規則**：一條 `app_metric > on(tenant[,version]) group_left(...) <該 recipe 的 user_threshold>` 涵蓋所有宣告該 recipe 的租戶——**規則數 = recipe 形狀數（shape），非租戶數**。

**效能誠實**：O(M)-與-N-無關**只對「共享指標」成立**。向量化消掉「同指標扇出複製 N 條」，但**消不掉「不同 metric 必然不同規則」**——租戶 A 的 `order_created_total` 與 B 的 `payment_failed_total` 必生成兩條。故 custom-alert 規則數隨**自訂告警總數線性增長**，不享 rule pack 對 [benchmarks.md §2](../benchmarks.md) 的 O(M) 保證。**護欄三件組**：(a) 硬性 per-tenant cap 封頂（已實作：tenant-api 寫入檢查拒收、da-guard `custom_alert_over_cap` 與 validate-config `custom_alert_cap` 在 CI 擋）；(b) 全域 rule-count budget（**規劃中、未實作**——其值待以實測 rule-eval-duration 反推，故與該 benchmark 一同 defer）；(c) 壞 rule 只炸自己的規則檔 group + Prometheus 原生 rule group `limit` + promtool hard gate（已實作）。

**部署源 = live conf.d**：編譯源必須是 exporter 實際服務的同一份 conf.d，否則生成規則的 shape 對不上 exporter emit 的 `user_threshold` series → 規則永不 fire（靜默失效）。custom-alert pack 是**租戶自訂、非平台覆蓋**，故從平台的 rule-pack / alert count 統計排除（badge 不變）。

### 5. 驗證雙層：同一份 Go 程式，寫入時看單一租戶、CI 看整棵樹（prod image 不打包 promtool）

- **Layer 1 — tenant-api 的 Go preflight**（in-process、快、stateless per-tenant 輸入閘）：`PUT` 當下就驗租戶 recipe spec、無效回 HTTP 400 + `Violations[]`，**壞輸入不進 repo**。複用與 exporter **共用同一份** Go 驗證器（metric regex / reserved label / recipe·op·severity·mode·for·horizon enum / ratio-floor ∈(0, 0.5)（< 現狀 band）/ NaN·Inf）。
- **Layer 2 — CI 的編譯**（全域權威，stateful + promtool）：`da-guard compile-custom-alerts` 對整棵樹做向量化並產生規則，再以 promtool 檢查模板——唯一握有全域 SOT 的權威 gate。它判定條目用的是 Layer 1 那同一份 Go 程式，兩層的差別只在看到的範圍：單一租戶的寫入，或整棵樹。

*Trade-off*：**prod image 不打包 promtool**。租戶從不寫 PromQL、recipe 模板平台 authored，故 valid spec → valid PromQL；promtool 只多抓「模板 regression」= 平台問題，留 **CI golden** 守，不放進請求路徑（避免熱路徑 subprocess 與 image 膨脹）。「整棵樹的 shape 數與規則數」這類需要全域樹的檢查 defer 至 CI——tenant-api 本地磁碟在 GitOps 真空期不是全域 SOT，熱路徑 tree-walk 會用過期樹 false-pass。

### 6. Silent 與 routing 隔離：複用既有 sentinel + inhibit

custom-alert 的 `mode`（page / silent）label 經 `group_left` 一路帶到 alert。Alertmanager 端的消費：

- **Silent 走 sentinel + inhibit，不走 route-to-null**——這是刻意複用 [ADR-003](003-sentinel-alert-pattern.md) 既有的三態 silent 典範。編譯器注入一條全域 sentinel `CustomRecipeSilent`（severity=none，從 `user_threshold{mode="silent"}` 導出），Alertmanager inhibit 以 `equal:[tenant, name]` 只抑制該 (租戶, recipe) 的通知；靜音的告警仍是 Prometheus 的 `ALERTS{...}` series → **silent ≡ dashboard-only**。選 inhibit 而非 route-to-null：與平台一致、AM UI 可見抑制狀態、且 dict-keyed 的 route-to-null 在繼承上會破壞語意（見否決）。sentinel **本身**帶靜態 `component="sentinel"` 判別標籤、由 platform-static sentinel-sinkhole route（`continue:false`，在 enforced/tenant route 之前）接走——sentinel 是 inhibit source 與儀表面、不是通知；無此標籤時它會因帶 `tenant` label 落入租戶主 route 產生 severity=none 通知噪音（[#1095](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1095) 修正，全平台 sentinel 一體適用）。
- **routing 隔離**：custom 告警帶靜態 `component="custom"` label（`component` 值按子樹保留——custom／sentinel／synthetic-probe——且精確 match，無歧義）；Alertmanager route **居首 + `continue:false`** 截在平台 NOC route 之前，避免租戶告警風暴灌進平台 NOC。`group_by` 用 `[tenant, alertname]`（alertname 已含 shape，不揉成一團）。page-mode 的 firehose receiver MVP 為隔離的空 receiver（outbound 接 log backend 而非限流的 IM，避 429→queue→OOM）。

### 7. Forecast recipe：趨勢 / 耗盡預測（雙模式）

`forecast` 回應「磁碟 / 記憶體照趨勢未來 N 小時內會耗盡」這類真實需求。raw `predict_linear` 是公認的 false-positive 製造機（瞬時尖峰被線性外推），故由平台把 FP 來源堵死、封裝成參數化 recipe——這正是 recipe 模式相對「租戶自寫 PromQL」的核心價值。

- **單一 recipe、雙模式**：有 `capacity_metric` → 比例 mode（預測比例掉破 floor ∈(0, 0.5)；≥0.5 被現狀 band 削弱故拒）；無 → 原始值 mode（預測 gauge 穿越絕對門檻）。
- **lookback 不給租戶填**，平台推導 `lookback = max(2·horizon, 1h)`（整數秒）；租戶只填 `horizon`（enum，cardinality 鎖定）。理由：lookback 是專家旋鈕、暴露即最大 foot-gun，且 `horizon ≤ lookback` 結構恆成立、免額外驗證。
- **cold-start 資料量 gate**（`count_over_time(base[lookback]) > N`）擋剛部署時樣本太少的亂跳；**gauge-only**（counter 須先 rate）。
- **現狀 band（僅比例 mode）**：比例型 forecast 額外閘 `現狀餘量比例 < 0.5`（契約檔裡的現狀 band，§9），把純斜率告警收成「預測會低 **且** 現在已偏低」，擋掉高 headroom 磁碟的暫態寫入突波 / cold-start 陡坡誤報；並以 `clamp_min(…, 0)` 把外推到負的比例值夾回 0（負餘量無意義、避免 on-call 看到亂值）。租戶閾值不受影響（在 core 比對）。**強制**：比例型 forecast 閾值須 `< 0.5`——≥ band 會被 band 靜默削弱。band 的值只寫在契約檔（§9），兩個入口的反應不同：tenant-api 寫入時拒絕；CI 編譯時把該條目隔離、不部署並印警告，其餘照常編譯、rc 0。

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

### 8. Recipe 生命週期治理：active / deprecated / eol

Recipe 是平台 authored 的；其 `status` 治理租戶能否續用，是 **RECIPE 版本控管**（有別於能力 A 的 APP `version` label）。狀態寫在 recipe 契約檔（§9）：tenant-api 的寫入閘、portal 的標示、治理文件 `rule-packs/recipes/*.yaml` 與 info-metric 都由它產生，**不手寫多份**。

- **active**：正常。
- **deprecated**：照常編譯 + 編譯器非致命警告 + portal 黃標；仍可新增。「可遷移、仍可用」。
- **eol**：**既有宣告照常編譯**（batch compiler 絕不因平台退役而砍掉已部署租戶的 rule、不靜默丟告警）；但**寫入端拒絕「擴張」**。

**eol 拒絕語意 = 「只擋擴張、不連坐」**：對每個 eol recipe，PUT 中使用它的實例數**不得超過現況**——擋「新增」「加量」「換用另一個 eol」，放行「改參數 / rename / 移除 / 既有續存」。刻意**不是**「擋掉任何含 eol recipe 的整次 PUT」：那是 full-overlay 連坐——凌晨救火時，一個兩年前、與災情無關的 eol recipe 會把租戶新增救命告警的整次寫入一起擋掉。eol 只凍結債務**增長**，從不動既有。

**為何在寫入閘強制**：擋擴張要比對「現況 vs 將寫入」，是個 stateful 檢查，故落在 tenant-api 的**共用寫入閘**——所有寫入端點（整租戶 PUT / custom-alerts PUT / 批次）一致覆蓋、無繞道。對照之下，CI / GitOps-direct compiler 看得到整棵宣告樹、卻分不出哪些是新宣告 → 只能 warn，不能 hard-reject。

**債務可見化**：編譯器為每個使用中的 shape 發一條靜態 info-metric `custom_recipe_info{recipe_id, recipe, status}`，讓 SRE 把「用量」接上「狀態」畫技術債燃盡圖：

```promql
count by(recipe_id)(count by(recipe_id, tenant)(user_threshold{component="custom"}))
  * on(recipe_id) group_left(recipe, status) custom_recipe_info
```

（內層 `count by(recipe_id, tenant)` 先去重租戶——`recipe_id` 不含 severity，直接 `count` 會把同一租戶的 warning + critical 算成兩個。）退役本身仍是 **SRE 手動**：metric 給視圖、不自動下架。

**eol ≠ 緊急下線**：若一個 recipe **有害、須立即移除**，工具不是 eol（它讓既有 rule 續活），而是**從 recipe library 刪掉定義**——既有實例編譯直接 fail，硬性強制移除。

### 9. Recipe 規則只有一份：Go 實作與 recipe 契約檔

一條自訂告警從寫入到產生規則，會經過 tenant-api 的寫入檢查、exporter 送出門檻、CI 編譯規則與 recipe-preview 的預覽。這幾處判斷「這條合不合法、算哪個 shape、產生什麼 PromQL」改用**同一份 Go 程式**（`components/threshold-exporter/app/pkg/config/`）：

- tenant-api 與 exporter 直接呼叫它；
- CI 編譯呼叫 `da-guard compile-custom-alerts`（輸出 JSON），Python 端 `compile_custom_alerts.py` 只負責讀寫檔案（寫入一定要指定 `--out`，結果為空時不覆蓋有規則的規則包，除非加 `--allow-empty`）、`--check` 比對與輸出序列化；
- recipe-preview（以近期資料試算一條 recipe 會不會觸發的服務）的映像帶 da-guard，預覽與部署用同一份判定，不會預覽一條 exporter 會拒收的 recipe。

portal 的 recipe 表單在瀏覽器裡執行，不跑 Go：它只做即時提示，界限讀下面的產生物，合不合法以 tenant-api 的寫入檢查為準。

規則裡的**資料**——各 recipe 可帶的欄位、必填欄位、不收的欄位與固定的運算子、各欄位的預設值、各欄位的列舉（recipe、op、mode、for、horizon、group_by、slo_period …）、metric、label、window、name 的格式、租戶不可當 selector 的保留標籤、p99_latency 的 quantile 範圍、forecast 的現狀 band 與比例下限、slo_burn_rate 的 `min_events` 預設與上限、生命週期狀態、SLO 每個嚴重度的偵測窗口、每個預算期間（28d、30d）的快慢兩級燒錯倍數、每租戶自訂告警上限——寫在一份 **recipe 契約檔**，放在同一個 Go package，以 `go:embed`（Go 在編譯時把檔案內容嵌進執行檔的機制）嵌入。exporter、da-guard 與 tenant-api 是 Go 程式，經同一個 package 直接讀它，不另存副本；其他需要這些資料的地方都由它產生，CI 重新產生後比對，有差異就紅：

| 產生物 | 用途 |
|---|---|
| `tenant-config.schema.json` 的 `_custom_alerts` 條目定義（列舉、格式、預設值與各 recipe 的必填規則） | 編輯器提示、validate-config 的 schema 檢查 |
| portal 的 `recipe-enums.json`、`recipe-status.json` | recipe builder 的選項與狀態標示 |
| validate-config 讀的每租戶自訂告警上限（`_lib_constants.py`） | `custom_alert_cap` 列 |
| `rule-packs/recipes/*.yaml` 的生命週期狀態、參數必填與否、列舉與預設值 | 給人讀的治理文件 |

自訂規則 lint（`lint_custom_rules.py`）的查詢範圍上限與 recipe-preview 的 `for` 分鐘數對照目前仍各自手寫；兩者預定在 recipe-preview 改走 Go 時改由契約產生（前者由 horizon 最大值推得）。

portal 表單裡手寫的檢查改讀產生物，不再自己寫界限。tenant-api 寫回 YAML 時用的欄位順序與「哪些欄位以字串寫出」也改讀契約檔。

為什麼不用多份實作加比對測試：同一條規則寫成多種語言，比對測試只能抽樣，抽不到的地方會各自演變。Go 與 Python 兩份實作就曾這樣分歧：Go 不准租戶以 `mode` 當 selector，Python 的保留標籤清單沒有它；SLO 的偵測窗口寫在 Python、配對的燒錯倍數寫在 Go，一個語意拆在兩種語言。只留一份實作，這類差異就沒有地方發生。業界的 [Sloth](https://github.com/slok/sloth)、[Pyrra](https://github.com/pyrra-dev/pyrra)（兩者都從 SLO 宣告產生 Prometheus 規則）也是單一 Go 程式，驗證與產生規則共用同一份程式碼。

## 資料流：Ingest → Define → Compile

- **Ingest**——租戶 app metric 進 Prometheus + scrape-time 烙 `tenant` / `version`。複用既有 `tenant-exporters` job + 平台預設 relabel 把 `app.kubernetes.io/version` → `version`（集中式，不下放 per-tenant）。
- **Define**——在租戶條目、根目錄平台檔的 `tenants:` 或 profile 填 recipe 表單，存成 declarative YAML；每個租戶取其中一處（見 §3）。
- **Compile**——recipe → 向量化 `group_left` 規則 → version graceful-join → promtool gate → 規則檔隔離 → GitOps 部署（operator manifest + ConfigMap projected volume + Prometheus reload；無 ArgoCD/Flux）。

## 復用既有機制（為何兩能力同住一份 ADR）

| Custom Alerts 需要 | 復用的既有資產 |
|---|---|
| scrape ingestion + version 烙印 | `tenant-exporters` job + 平台預設 `app.kubernetes.io/version` relabel |
| version graceful join | version-aware normalize layer 的 `version=~"\|default"` 左外連接 |
| 跨租戶隔離 | namespace→`tenant` scrape-stamp + prom-label-proxy（[ADR-020](020-tenant-federation.md)） |
| 一組租戶共用的政策 | profile（`_profiles.yaml` + 租戶的 `_profile:`） |
| 向量化 1-rule-蓋全租戶 | rule pack 既有 `on(tenant) group_left` O(M) pattern |
| silent | [ADR-003](003-sentinel-alert-pattern.md) sentinel + inhibit |
| 寫入驗證 / 預設融合 / 維護抑制 | tenant-api `validate()` + `MergeTenantWithRootDefaults` + `user_state_filter{filter="maintenance"}` |

唯一 **net-new 核心**：recipe 庫 + 參數 schema + recipe 編譯器 + discovery proxy + cost cap + recipe 編輯 UX。

## 關鍵 Trade-off

核心判斷是 **reuse-over-build**：目標能力 90% 已存在於既有 dimensional 機制。額外打造新 schema 的唯一實質增益是 authoring 分組，但代價是觸碰 hot-reload critical path + 引入功能重複的第二條 default-注入路徑 + 把向下相容從「自動成立」變成「需驗證」。因此一律以最小 net-new surface 復用既有機制，把成本誠實標出（custom-alert 規則數對 unique metric 是線性，用 cap 封頂），換來平台與租戶可自訂業務告警而不寫 PromQL。

## Consequences

**變容易**：升版閾值 cutover 與 K8s rolling update 自動對齊、無時序漂移；「現在跑什麼版本」可由 `count by(version)(<app metric>)` 直接答出；YAML 不再累積歷史生效日；租戶自訂告警不需平台逐一加 rule。

**變難 / 新增的 failure mode**（及其化解）：

- **observed-but-not-declared**（metric 在跑、沒宣告該版號閾值）→ 動態降級**架構性消解**（自動 fallback 到 default、不丟 series）。殘餘：typo 版號靜默落 default，靠 orphan 偵測抓。
- **`default` 命名碰撞**——da-guard **禁止顯式 `default`**（保留給 fallback），避免與未版本化閾值在同 bucket 取 max 時的歧義。
- **cardinality 截斷必須確定性**——per-tenant guard 截斷前須對 dimensional keys 確定性排序（無版號 / `default` 優先保護），否則 Go map 隨機序會讓被截的版號隨 scrape 忽隱忽現 → **alert flapping + 重複 page**。
- **共享 `user_threshold` 跨 pack 洩漏**——非 pilot pack 的 normalize matcher 加 `version=~"|default"`（只收無版號 / default），CI guard 失效時仍保既有告警安全（防 double-count）。
- **Dashboards / Portal query 假設無 version label**——租戶開始寫 version key 後，未聚合 query 會多出帶 version 的 series → 須審 Grafana / portal panel。
- **編譯器與 exporter 的來源要一致**——編譯器自己走樹時會為 exporter 不送的 `_custom_alerts` 產生規則（對沒有門檻送出的租戶永遠不會觸發），也會漏掉 exporter 有送的（例如 profile）。來源只由 exporter 判定，編譯器讀的就是 exporter 取得的清單（§3）。診斷工具也只看這一份：`describe_tenant`（列出一個租戶最終設定的 CLI）顯示 exporter 為每個租戶送出的 `custom_alerts` 與每條的狀態，不顯示把子目錄 `_defaults.yaml` 與租戶自己的清單合起來的視圖（exporter 從不送出那種合併結果）；「這次 conf.d 變更影響什麼」只由 config-diff 回答（[ADR-037](037-config-diff-compares-rendered-output.md)），回答同一問題、讀 `describe_tenant` 輸出的 blast_radius 退役。
- **遷移時規則檔的位元組可能改變**——編譯器改到 Go 時，驗收標準是產生的規則檔以 YAML 解析後內容相同、alert 與 recording rule 名稱及 recipe_id 集合相同、promtool golden 測試通過，不要求逐位元組相同；committed 的規則檔在那次一併重新產生。
- **recipe-preview 映像多帶一支 da-guard**——預覽要用與部署相同的判定，映像因此變大（da-guard 是單一靜態 Go 執行檔）。

**關鍵不變式**（驗收契約，非 tracking checklist）：(1) 既有未寫 version 的租戶升級後行為 100% 等價（無 series 數變動）；(2) over-cap 截斷跨多次 scrape 截掉的是固定版號（無 flapping）；(3) `mode: silent` recipe 觸發時不進 PagerDuty / Slack、只留 Grafana 痕跡；(4) rolling update 結束、舊版 metric 消失後，正在燒的告警能正常收到 Resolve（不留殭屍告警）；(5) version-aware metric 上的 recipe 自動 graceful-join、label 缺時落 default、不產 NaN / 空集。

## 否決的替代方案

- **`ScheduledValue.from/until` 絕對日期 schema 擴展** — YAML 累積無意義生效日（過期 `from` 永遠 true）+ 雙寫 atomicity 風險。把時間軸吞進 declarative config 是結構性錯誤。
- **`POST /active-version` 寫狀態 API** — 引入第二個 state 破壞 single SOT；rolling 中段呼叫造成 transient 不對齊。Metric 帶 version 流進來就是 SOT。
- **Scheduled PR Merge orchestration** — Git 瞬時二元 merge 無法 align K8s 5–10 分鐘漸進發佈 + GitOps 傳遞鏈延遲 + helm rollback 不反向 revert Git PR。
- **PromQL normalize 用 `or on() vector(0)` 補假值** — 破壞下游聚合、對「值為 0 即告警」rule 製造 false positive。正解 = `label_replace(..., "version", "default", "version", "^$")`。
- **租戶直接寫 PromQL** — 違反 declarative-only 鐵則；概念正確但被 adapt 成 dimensional-label / recipe 方案。
- **Silent 走 route-to-null receiver** — 與 [ADR-003](003-sentinel-alert-pattern.md) 既有 inhibit 典範不一致、AM UI 看不出抑制狀態；改複用 sentinel + inhibit。
- **讓 exporter 實作 `_defaults.yaml` 子樹的聯集繼承** — 能保住「domain 寫一次、套整棵子樹」，但今天寫在 `_defaults.yaml` 的 recipe 會一次對整棵子樹生效（例如 absence recipe 對所有沒送該 metric 的租戶同時觸發），而租戶要怎麼選擇不套用沒有定義。
- **編譯器另外實作一份與 exporter 相同的來源規則** — 改動最小、不需要在編譯環境放 da-guard，但兩份實作仍會各自演變而分歧。
- **保留 Python 編譯器，以 Go／Python 比對測試對齊** — 改動最小，但比對只能抽樣，抽不到的差異照樣漂移（§9 裡 `mode` 的例子就不在抽樣範圍內）；每加一條規則就要在兩種語言各寫一次。
- **契約檔用 JSON Schema 表達，Go 讀 schema** — schema 本來就要給編輯器與 validate-config 用，但 Go module 沒有 JSON Schema 函式庫，要嘛新增依賴、要嘛手抄一份；schema 也是今天被程式碼釘住的一方，改由它驅動程式會把方向倒過來（與 [ADR-038](038-unread-keys-judged-by-readers.md) 否決「讓 schema 當登錄表」同理）。
- **把 `_custom_alerts` 從 list 改 dict（以名為 key）** — list 形態貫穿整個引擎（Go exporter / tenant-api / portal / 編譯器），改形態的 blast radius 不成比例；每個租戶只取一個來源，也不需要以名稱合併。

## 與排程式閾值（§2.6）的界線

[config-driven.md §2.6](../design/config-driven.md) 的 `ScheduledValue.overrides: [{window, value}]` 是 **recurring 時間窗口**機制，與本 ADR 刻意分開、可同時作用於同一租戶：

| 維度 | §2.6 排程式閾值 | ADR-024 Version-Aware |
|---|---|---|
| 切換軸 | **時間**（recurring 窗口，看 wall-clock） | **狀態 / version label**（不評估時間） |
| 觸發來源 | UTC 時鐘到點，每日重複 | app 升版後 metric 帶新 version，one-time cutover |
| 典型場景 | 「夜間 22:00–06:00 放寬到 200」 | 「v2 上線後 CPU 閾值由 80 收緊到 60」 |

兩者正交：§2.6 處理週期性時段，本 ADR 處理一次性的版本對齊。

## Addendum：磁碟與 PVC 告警 (v2.10.0)

讓租戶對磁碟的容量與吞吐設告警，回應資料庫客戶遷移時最常見的「磁碟快滿了」需求（[#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692)）。沿用既有的 recipe（forecast / ratio / threshold / rate），不新增種類，但有幾件事和一般 recipe 不同。

**兩種信號、兩種粒度。**

| 想告警的事 | 用的指標 | 粒度 |
|---|---|---|
| 磁碟快寫滿 / 容量利用率 | `kubelet_volume_stats_*`（available / capacity / used） | **每顆 PVC**（帶 `persistentvolumeclaim`） |
| 磁碟吞吐 / IOPS | `container_fs_*`（reads / writes） | **每個容器**（不是每顆 PVC） |

吞吐指標來自容器檔案系統統計，它的 `device` 是節點的區塊裝置、對不到特定 PVC——單一 PVC 的 Pod 大致等價，多 PVC 就切不開。所以「精準到某一顆 PVC」目前只有容量這側做得到。

**前提：磁碟容量指標需要 CSI driver。** kubelet 只對「實作了 volume stats 介面（`NodeGetVolumeStats`）」的儲存後端吐 `kubelet_volume_stats_*`。一般雲端的 CSI driver（EBS、GCE PD 等）都有；但 kind 預設的本機儲存（hostPath）完全不吐——驗證時實測到 751 個 kubelet 指標、卻 0 個 volume stats。裝上 CSI driver 後，`kubelet_volume_stats_*{namespace, persistentvolumeclaim}` 隨即出現。**所以磁碟容量 recipe 的硬前提是：PVC 由支援 volume stats 的 CSI driver 提供**（多數雲端環境都符合）。這條會寫進 recipe 使用說明與 BYO 整合前提。

租戶歸屬沿用平台既有的 1:1 做法——抓取階段把 `namespace` 當 `tenant`、指標名保持不變；N:1 / 1:N 的政策見 [ADR-006 §Addendum](006-tenant-mapping-topologies.md)。

**關鍵正確性：逐顆 PVC 判斷，別被加總掩蓋。** 一個租戶可能掛好幾顆 PVC。如果把指標用 `by(tenant)` 加總再比閾值，就會出事：

> 一顆 10GB 的設定碟已經 99% 滿、一顆 500GB 的資料碟才 10% 滿——加總後「整體還有 88% 空間」，告警永遠不觸發，但那顆小碟其實快把資料庫弄垮了。

所以磁碟容量 recipe **逐顆 PVC 判斷、任一顆達標就觸發**：聚合時保留 `persistentvolumeclaim` 這個維度，閾值仍是每租戶一個、套用到該租戶的每顆 PVC，告警會標出是哪顆碟。為了不讓指標基數失控，能保留的維度只有 `persistentvolumeclaim` 這一個。

**兩道防線：讓設錯的告警大聲壞掉、而非悄悄失效。** 如果租戶對一個根本沒被抓取、或抓了卻沒帶 `tenant` 的指標設告警，那條告警會編譯成功、卻永遠不觸發——這是最危險的失效。兩道防線堵住它：

- **編譯期（CI）**：直接解析 Prometheus 的抓取設定，確認 recipe 用到的每個指標（包含 ratio 的分母、forecast 的容量指標）確實被抓、而且該抓取工作有把它歸屬到 `tenant`，否則 CI 報錯。做法是以抓取設定本身為準，不另外維護一份會過期的清單。它只能保護對齊本平台抓取設定的情況；客戶自帶的 Prometheus 設定看不到，由下一道補上。
- **執行期看門告警 `CustomRecipeDiskInert`**：租戶設了磁碟告警、Pod 也在跑，但歸屬後的磁碟指標一直沒出現，就觸發。這把「拓撲不支援」「漏設歸屬」「租戶根本沒掛 PVC」這幾種情況，從悄悄失效變成有人會看到的告警。

v2.9.0 曾隨範例附過一條磁碟預測告警，引用的正是當時沒人抓取的 `kubelet_volume_stats_*`——它在測試夾具上看起來正常、在真實叢集裡卻從不觸發。本次的抓取啟用加上這兩道防線一併修掉。

## 更新註記（post-acceptance）

> 本節記錄 accepted 之後由後續 ADR／PR 帶入、尚未併進上文的變化。

- **Version pilot 第三族 `cpu_throttle` 機械已鋪、exact branch 依 pilot allowlist 暫 inert**——§2 的 pilot 範圍（`container_cpu` + `container_memory`）不變，但 [#944](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/944) 已為 `cpu_throttle` 鋪好同款 version 機械（`rule-packs/rule-pack-kubernetes.yaml` 的 normalize pair + Route-2 exact-or-fallback per-severity cores）。`cpu_throttle` 尚不在 version pilot allowlist（`resolve.go` 的 `pilotVersionMetrics` 僅 container/cpu + container/memory），租戶無法宣告 per-version throttle 閾值，exact-match branch **依設計暫 inert**、一律走 `version="default"` fallback；未來納入 pilot 是一行 allowlist 變更、非規則重寫。

## Cross-Reference

- [ADR-006: 租戶映射拓撲](006-tenant-mapping-topologies.md) — 基礎設施指標 1:1 歸屬政策 + N:1/1:N defer。
- [ADR-003: Sentinel Alert 模式](003-sentinel-alert-pattern.md) — silent / 三態的 sentinel + inhibit 基礎。
- [ADR-008: Operator-Native 整合路徑](008-operator-native-integration-path.md) — declarative-only 鐵則。
- [ADR-017: `_defaults.yaml` 繼承](017-defaults-yaml-inheritance-dual-hash.md) — 閾值的目錄樹繼承（`_custom_alerts` 不走這條，見 §3）。
- [ADR-020: Tenant Federation](020-tenant-federation.md) — 跨租戶讀路徑隔離（prom-label-proxy）。
- [ADR-031: SLO burn-rate recipe](031-slo-burn-rate-recipe.md) — 第 7 個 recipe 與燒錯倍數。
- [ADR-037: config-diff 比較渲染結果](037-config-diff-compares-rendered-output.md) — conf.d 變更的影響範圍報告。
- [Version-Aware Thresholds 使用攻略](../scenarios/version-aware-thresholds.md) — 租戶宣告 + 平台 KSM 設定的操作面。
- [config-driven.md §2.x](../design/config-driven.md) — dimensional 閾值與 recipe 的活體規格。
- [Recipe would-fire 預覽設計](../design/recipe-would-fire-preview.md) — capability B 的 confidence last-mile（#657 P1 設計就緒）。
