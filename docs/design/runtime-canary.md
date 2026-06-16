---
title: "Runtime Canary 設計 — 自訂告警編譯管線的端到端活性保證"
tags: [architecture, alerting, canary, self-liveness, design]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
parent: architecture-and-design.md
---
# Runtime Canary 設計

> **Language / 語言：** **中文 (Current)** | [English](./runtime-canary.en.md)

<!-- Language switcher is provided by mkdocs-static-i18n header. -->

> ← [返回主文件](../architecture-and-design.md)
>
> **相關**：[ADR-025 告警平面自我存活性](../adr/025-alerting-plane-self-liveness.md)。本文是該 ADR「之後再說」表中 **runtime canary** 項的**設計就緒 (design-readiness)** 產出——常駐部署仍 defer，觸發條件見文末。

## 它補的盲點

平台對「自己的告警平面會不會無聲死掉」已有兩道防線，但**都看不到**租戶**自訂告警**那條編譯管線：

| 既有防線 | 證明什麼 | 看**不到**什麼 |
|---|---|---|
| **D1 Watchdog**（[ADR-025](../adr/025-alerting-plane-self-liveness.md)，`vector(1)` + 外部 dead-man's-switch） | Prometheus 引擎活著、且 Alertmanager → receiver 投遞鏈通 | 引擎活著、但租戶自訂告警的**資料側/規則側**悄悄斷了 |
| **pint**（CI 規則靜態檢查） | 規則在**作者時**語法/語意正確（如聚合不會砍掉 template 用到的 label） | 部署後的**執行期**——規則有沒有真的拿到資料、有沒有被載入 |

兩者之間有一條**端到端的活性**沒有人守：

```
租戶 conf.d 宣告 → threshold-exporter 發 user_threshold{component="custom"} →
  Prometheus scrape → compile_custom_alerts 產出規則 → Prometheus 載入並評估 →
  Alertmanager 路由
```

這條鏈的**任何一環**靜默斷掉，症狀都一樣：**告警不再觸發**——而「沒有告警」和「一切健康」長得**一模一樣**。具體的靜默死法（本 repo 都踩過或可預期）：

- exporter 停止發 `user_threshold`（conf.d parse 壞了、[#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) collector 例外）→ 規則的 threshold 側永遠空 → join 永遠空。
- 編譯器產出空集合 / drift（規則被改沒了，但沒人發現）。
- scrape gap（exporter 還活著但 Prometheus 抓不到）。
- 規則沒被 Prometheus 載入（rule-pack 投影 / reload 壞了，[#731](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/731)-class 的 silent-strip）。

Watchdog 是平台的「心跳脈搏」，但它是一條 `vector(1)` 平台規則——**不經過**上面那條租戶編譯鏈，所以抓不到這些。**這正是 runtime canary 存在的理由。**

## 設計：常駐假租戶 + 必觸發 + dead-man's-switch

canary 不是新機制，而是把平台**既有**的自訂告警管線**拿來當探針**——這是它的價值所在：它走**完全相同**的那條鏈，所以鏈斷在哪都抓得到。

1. **一個保留的假租戶**（如 `platform-canary`），在 conf.d 宣告一條**必觸發**的 `threshold` recipe：對一個恆定為 1 的 heartbeat gauge 設 `> 0`——`1 > 0` 永遠成立，所以 canary 的 `Custom_` 告警**持續觸發**。
2. **`mode: silent`**：canary 永遠不 page 真人——它只是一條 dashboard-only 的 `ALERTS` series（經 ADR-003 sentinel + inhibit 抑制通知，不是 route-to-null）。**它的「存在」本身沒有意義；有意義的是它「消失」。**
3. **dead-man's-switch meta-alert** `CustomAlertPipelineCanaryDown`：當這條必觸發告警**停止**時 page NOC。

```yaml
# conf.d/platform-canary.yaml — 保留租戶，走真實 GitOps 編譯鏈
tenants:
  platform-canary:
    _custom_alerts:
      - recipe: threshold
        name: pipeline_heartbeat
        metric: canary_pipeline_heartbeat   # 平台恆定發 1 的 heartbeat
        op: ">"
        window: 5m
        threshold: "0:warning"
        mode: silent                        # 永不 page；只當 dashboard series
```

meta-alert 是一條**手寫的平台規則**（不是編譯產出），且**刻意盯 canary 的 core recording rule**，而非 `ALERTS{...}`：

```yaml
- alert: CustomAlertPipelineCanaryDown
  expr: absent(custom:threshold__canary_pipeline_heartbeat__gt__w5m__for1m:warning:core{tenant="platform-canary"})
  for: 5m          # 須 > canary 的 for:1m + scrape/eval 餘裕
  labels: { severity: critical, component: platform-canary }
```

為何盯 core record 而非 `absent(ALERTS{...})`：(a) `absent(ALERTS)` 會把 matcher label（`alertstate="firing"` + 一個內嵌的 `alertname`）漏到 meta-alert 上；(b) 若用 tenant-wide 的 `ALERTS{tenant="platform-canary"}` matcher，**`CustomRecipeSilent` sentinel 也會為這個租戶觸發**（它只看 `user_threshold` 存不存在），於是 heartbeat scrape 斷了、core 沒了，但 sentinel 還在 → matcher 仍被滿足 → **漏報**。盯 core record 的 `absent()` label 乾淨，且 exporter 停發 / scrape gap / **該規則**被編譯漂移掉這幾類靜默死亡都抓得到。

> **為何不把 `absent()` 乘上恆存指標來「錨定標籤」**：有人會建議寫成 `absent(…core…) * on() group_left() (up{job="prometheus"}*0+1)` 以繼承拓樸 label。**不採**——dead-man's-switch **絕不能依賴一個正向訊號存在**：當 Prometheus 自我 scrape `up{job="prometheus"}` 也在同一場故障中消失時，乘算結果變空 → meta-alert **不觸發**，正好在最該觸發時自廢；且 HA 下 `up` 為多 series → many-to-one join 報錯。本設計也**無此需求**：core 已 `by(tenant)` 聚合（沒有拓樸 label 可掉），而 `cluster` 等 external_labels 由 Prometheus 送往 Alertmanager 時**對 present/absent 一律均勻附加**，不存在 present/absent 間的 label 漂移。

> **誠實邊界（兩個它抓不到的情形）**：(1) meta-alert 自己也是經**同一條** rule-pack 投影載入的內部規則，所以「**整份** rule-pack 載不進來」這種失敗會**同時**讓 canary core 與 meta-alert 一起消失——這個「誰看守看守者」的遞迴盲點由 Watchdog 的**外部** dead-man's-switch 補（見下節分工）；canary 守的是「Prometheus 規則照常載入、但**該租戶的資料 / 該規則**這條路斷掉」的較窄情境。(2) core 帶 `unless … user_state_filter{filter="maintenance"}`，所以保留 canary 租戶若被誤置入 maintenance，core 會消失 → meta-alert **誤觸**——故保留租戶須一併排除於 maintenance window（見範圍邊界）。

## 為什麼 config 放 `conf.d/` GitOps，不寫死 `k8s/`

這是本設計唯一一個**有兩種看似合理選擇**的決策，結論由 canary 的目的反推：

- **寫死 `k8s/`（raw PrometheusRule / 直接塞 configmap）** → **繞過** conf.d scanner、編譯器、configmap regen——而這三個正是**最可能靜默壞掉**的環節。編譯器全死了 canary 還在燒 → **false green**，退化成「第二個 Watchdog」（只證 Prometheus→AM，Watchdog 已覆蓋）→ **失去意義**。
- **`conf.d/` GitOps + 保留租戶**（本設計）→ 走**真實**那條鏈，任一環斷 → canary 停 → meta-alert page。這才是 canary 的價值：**dogfood 它要守護的那條 pipeline**。

實務上，production 的 SSOT 是 [`components/threshold-exporter/config/conf.d/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/config/conf.d)——**exporter 與編譯器共用同一份 conf.d**（否則 `recipe_id` 對不上 emit）。保留租戶就是這裡的一個檔案，與真實租戶**零差別**地流過管線。

## 它跟 Watchdog / pint 的分工（互補，非重疊）

| 層 | 守護對象 | 機制 | 誰 page |
|---|---|---|---|
| **pint** | 規則**作者時**正確性 | CI 靜態檢查 | CI 擋 PR |
| **D1 Watchdog** | **引擎**活性（Prometheus 評估 + AM 投遞） | `vector(1)` → 外部 DMS | 外部 dead-man's-switch |
| **runtime canary** | 租戶自訂告警的**編譯→投遞**執行期活性 | 常駐假租戶必觸發 → `absent()` meta-alert | meta-alert（內部，但盯的是被守護鏈本身） |

canary 由**同一個** single-replica Prometheus 評估——它守的是「引擎活著、但租戶編譯鏈悄悄斷了」這個 Watchdog 看不到的**較窄**情境。「整個 Prometheus 死掉」仍是 Watchdog + 外部 DMS 的職責。三者**疊起來**才完整：作者時 → 引擎 → 租戶管線。

## 「壞租戶隔離」——誠實的兩層說明

ADR-025「之後再說」表原本把 canary 的信任資產寫成：「故意注入一個損壞的租戶設定，canary 仍**成功編譯**、繞過單點錯誤、正確送出」。**對照現行程式碼，這個 demo 的敘述是錯的**——本平台的「壞租戶不拖垮好租戶」其實是**兩個不同的層**達成的，不是「壞的還能編譯」：

**第一層 — 編譯/CI fail-closed + 租戶可定位。** 編譯是**整批**的：一個 schema 損壞的租戶設定會讓**整個** `compile_custom_alerts.py` 回 exit 2（[`compile_custom_alerts.py:210-215`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/compile_custom_alerts.py)），而 loader 的錯誤**指名出錯的檔案**（[`loader.py:79-107`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/loader.py)，`origin` = 宣告所在檔）。所以隔離靠的是**預防 + 可定位診斷**：壞設定被 CI 擋下、**永不部署**，而非「壞的照編、好的也照編」。**canary 在此沒有特殊魔法**——它跟所有租戶一樣受這道 fail-closed gate 保護。（租戶本來就**不寫 PromQL**，編譯器代寫，所以租戶無法注入會在執行期炸掉的表達式。）由此可知：一個**畸形的 recipe**（非法 window、無法解析的 threshold）在這一層即被擋、**永不進入執行期**——所以第二層的執行期隔離測試（demo case 2）餵的是壞**資料**（鄰居帶極端值），而非壞 recipe；「執行期還在跑的壞 recipe 鄰居」在這個架構裡**不存在**。

**第二層 — 執行期 per-tenant row 獨立。** 一個**語法合法但行為異常**的租戶（資料壞掉、指標缺失、閾值設錯）只影響**它自己**在向量化規則裡的那一列。每條規則的聚合都是 `max by(tenant, version)`（[`recipes.py:137-148`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/recipes.py)）、join 都是 `on(tenant[, version]) group_left`（[`recipes.py:234-249`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/dx/custom_alerts/recipes.py)），所以一個租戶缺失的 series **無法**把另一個租戶的列清空。**這才是「別人的錯拖垮我的告警」真正被擋下的地方**，而 canary 正是端到端地**證明這層活著**：它與所有租戶共用同一條向量化規則，所以(a) 鏈全域斷 → canary 停 → meta page；(b) 單一租戶壞 → canary 照燒 → 證明隔離成立。

> 這個更精準的兩層框架取代了 ADR 原本的單句 hand-wave；本設計把它變成可驗證的 demo（見下）。

## Demo（promtool，CI-run）

設計就緒**附帶一個可跑、不會 rot 的 demo**——不是一份會跟程式碼漂移的文件，而是經由**真實編譯器**產出、且在 CI 跑的 promtool 測試：

- [`tests/rulepacks/runtime-canary.rules.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/runtime-canary.rules.yaml)：canary 的規則鏈**逐字**取自 `compile_custom_alerts.py` 對上面那條 recipe 的輸出（證明它 dogfood 真實編譯鏈），加上手寫的 dead-man's-switch meta-alert。
- [`tests/rulepacks/runtime-canary_test.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/tests/rulepacks/runtime-canary_test.yaml)：三個 case 證明本文三項主張——
  1. **活性**：heartbeat 在 → canary 端到端觸發、meta 靜默。
  2. **隔離（containment）**：同 shape 的鄰居租戶帶極端值（9999）同時觸發 → canary 那一列仍是**自己的值（1.00）、不被污染**——兩列各自獨立（證明 join 不跨租戶混值）。
  3. **dead-man's-switch**：exporter 停發 `user_threshold` → canary 的 core record 消失 → `CustomAlertPipelineCanaryDown` 在 5m 後 page。

由 CI 的 promtool loop（`for t in tests/rulepacks/*_test.yaml`）執行；本地：`promtool test rules tests/rulepacks/runtime-canary_test.yaml`。

### try-local 現場跑法

要在 [`try-local/`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/try-local) 看到 canary**真的**穿過 exporter→Prometheus 觸發（而非 promtool 餵 series），需要兩步——之所以是「步驟」而非「現成」，正是下一節 defer 的原因：

1. 讓 try-local 的 threshold-exporter 用**含 S3 的映像**（source-build，比照 `tenant-api` 已有的做法；或 `EXPORTER_TAG` 指向 post-S3 release）——published `v2.8.0` 映像早於 S3，不發 custom `user_threshold`。
2. 在 `try-local/seed/conf.d/` 加保留 canary 租戶（上面的 recipe），並用 `seed/push-metrics.sh` 推 `canary_pipeline_heartbeat=1`。

> exporter 的**原始碼已支援** `_custom_alerts`（`custom_alert_collector_test.go`：一條合法宣告即發 `user_threshold{component="custom",…}`），production 也已用同一份 conf.d 串起整條鏈。**能力不缺**；try-local 缺的只是把 published 映像換成 post-S3 的那一版。

## 為什麼「常駐部署」仍 defer（觸發條件）

**設計就緒度高**——管線在 production 已串通（S3 已落地），上面的 recipe 經真實編譯器驗證可跑，demo 已綠。**defer 的不是能力，是營運承諾**：

- 一個**常駐的 heartbeat 來源**（發 `canary_pipeline_heartbeat=1`）。**首選做法是 threshold-exporter 的自我宣稱（self-metric）**——exporter 是我們 own 的 Go 原始碼，啟動時於 `/metrics` 直接吐 `canary_pipeline_heartbeat 1` 即可，stateless、零運維，免 Pushgateway / Cron 外部依賴（recipe 仍走 conf.d GitOps，故 conf.d→編譯路徑照樣被測）。這是 canary 唯一的小型常駐元件。
- **把 meta-alert 接上真實 on-call**（`CustomAlertPipelineCanaryDown` → 真的 pager，而非 demo sink），**並把它納入 Watchdog 同款的 inhibit 免疫**——meta-alert 是 `severity:critical`，但現行 `assert_watchdog_inhibit_immunity` 只護 `WATCHDOG_IDENTITY_LABELS`（`alertname="Watchdog"` / `severity="none"`，[`_grar_validate.py:101`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/_grar_validate.py)），**不**涵蓋它。若不補，一條廣域 `ClusterDown → 抑制所有 critical` 的 inhibit 會在「基礎設施故障**同時**打斷自訂告警管線」這個最該收到訊號的相關性故障下，把管線死亡訊號在 Alertmanager 直接**窒息**。故常駐部署須把 `CustomAlertPipelineCanaryDown` 一併登記進該免疫檢查（或給它專屬防禦標籤並於廣域 inhibit 顯式排除）。

**defer 軸**（與 ADR-025 其他項一致）：門檻是**被取代/整合的外部成熟 incumbent 的標準**，不是我們內部成熟度。runtime canary 的成熟監控產品對應物（如 synthetic monitoring / blackbox 自我探測）是 evaluation-time 要拿得出的可信度——**本設計 + CI demo 已滿足**；常駐部署 defer 到：

- **觸發 A**：重大規則編譯重構 / 多租戶路由大改前——先佈署當安全網。
- **觸發 B**：首個 production 自訂告警「規則評估悄悄失敗」事件——事後佈署防再犯。

## 範圍邊界

- **不做** canary 自己的告警內容正確性測試（那是各 recipe 的 promtool golden 的事）；canary 只證**管線活性**。
- **不做** HA——canary 由 single-replica Prometheus 評估；整個引擎死掉是 Watchdog + 外部 DMS 的職責（[ADR-025 §D3](../adr/025-alerting-plane-self-liveness.md)）。
- **不取代** Watchdog 或 pint——三者互補（見上表）。
- 保留租戶 id（`platform-canary`）須排除於 tenant-count / chargeback / quota / **maintenance window**（後者：maintenance 會讓 core 消失而誤觸 meta-alert，見上方誠實邊界）；命名前綴依 operator 慣例可調。
