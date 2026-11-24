---
title: "ADR-029: 租戶自訂告警跨租戶查詢隔離 — 編譯期邊界中和為主、評估期 ruler 隔離延後"
tags: [adr, threshold-exporter, custom-alerts, security, multi-tenant]
audience: [platform-engineers, security, sre]
version: v2.9.0
lang: zh
id: ADR-029
tracking_kind: adr
status: accepted
domain: threshold-exporter
created_at: 2026-07-05
updated_at: 2026-11-24
---
# ADR-029: 租戶自訂告警跨租戶查詢隔離 — 編譯期邊界中和為主、評估期 ruler 隔離延後

## 狀態

✅ **Accepted**（2026-07-05）

2026-11-24 修訂：改寫為現行行為。決策不變。

2026-11-24 修訂：selector 的值改為在寫入與產生路徑同步確認 Prometheus 載入得了，原本「`selectors_re` 的正規表示式語法沒有同步驗證」的已知限制移除。

本 ADR 只有中文版（依語言政策，部分 ADR 不另製英文版）。

## 決策

**租戶自訂告警不能讀到別的租戶的資料，這件事在「編譯期」守住，不在「評估期」守住。**

- **編譯期**指租戶宣告的自訂告警（`_custom_alerts`）被轉成 Prometheus 規則的那一步；**評估期**指 Prometheus 之後依規則查詢資料的那一步。
- 平台維持一個共用的 Prometheus 與**向量化規則**：同一種告警形狀（shape，即 recipe 加上參數的組合）不論幾個租戶宣告，都只產生一組規則，規則跨所有租戶評估，再以 `tenant` 標籤配對各租戶自己的閾值（見 [ADR-024](024-version-aware-threshold-via-dimensional-label.md) §4）。
- 租戶填的每個值，在寫進規則**之前**就驗證：只能選平台寫好的 recipe，名稱類欄位（metric、告警名、標籤名）限字元集，列舉欄位限列舉，數值限格式與值域；selector 的值拒絕模板字元、其餘經跳脫後寫入，不能釘住平台保留的標籤；值不能含 Prometheus 字串常值拒收的字元（無效 UTF-8 與 U+FFFD），`selectors_re` 的值還必須能被 Prometheus 編譯成正規表示式。產生出來的標籤與 annotation 只能含平台自己寫的模板片段。
- 把隔離移到評估期（換成每個租戶各自查詢範圍的 ruler）**目前不做**，四個條件任一成立時重新評估，見下方「重新評估的條件」。

### 範例：一個合法宣告與它產生的規則

租戶 `db-b` 在 conf.d 宣告一條自訂告警，只填平台定義的欄位，不寫 PromQL：

```yaml {.quote src="components/threshold-exporter/config/conf.d/db-b.yaml"}
tenants:
  db-b:
# ...
    _custom_alerts:
      # threshold (page): MariaDB connection count climbing toward the cap.
      - recipe: threshold
        name: mariadb_conns_high
        metric: mysql_global_status_threads_connected
        op: ">"
        window: 5m
        threshold: "150:warning"
        mode: page
```

編譯出的規則不含任何租戶 id。指標端跨所有租戶聚合，再以 `on(tenant, version)` 配對 threshold-exporter 為各租戶送出的閾值；沒有宣告這個形狀的租戶沒有閾值序列，配不到，就不會觸發：

```text {.quote src="rule-packs/rule-pack-custom-alerts.yaml"}
  - record: custom:metric:threshold__mysql_global_status_threads_connected__gt__w5m__for1m
    expr: label_replace(max by(tenant, version, da_cluster) (mysql_global_status_threads_connected), "version", "default", "version", "^$")
  - record: custom:threshold__mysql_global_status_threads_connected__gt__w5m__for1m:warning:core
    expr: |-
      (
        (
          custom:metric:threshold__mysql_global_status_threads_connected__gt__w5m__for1m
          > on(tenant, version) group_left(name, mode)
            custom:threshold:threshold__mysql_global_status_threads_connected__gt__w5m__for1m{severity="warning"}
        )
# ...
      unless on(tenant)
      (user_state_filter{filter="maintenance"} == 1)
```

同一個租戶若在 metric 欄位寫進 PromQL（`app_queue_depth{tenant="db-a"}`），或用 selector 釘別的租戶（`selectors: {tenant: db-a}`），該條宣告會被拒絕、不產生規則，其餘宣告照常編譯。以 `scripts/tools/dx/compile_custom_alerts.py` 編譯一棵含這兩條的設定樹，輸出節錄（路徑已截短）：

```text
  ⚠ tenant=db-b _custom_alerts[1] name='evil': rejected by the exporter, not compiled: selector label "tenant" is reserved and may not be pinned
  ⚠ tenant=db-b _custom_alerts[3] name='evil3': rejected by the exporter, not compiled: metric "app_queue_depth{tenant=\"db-a\"}" is not a bare metric name (^[a-zA-Z_][a-zA-Z0-9_]*$); label filtering must use selectors/selectors_re, never inline PromQL
✅ Compiled 1 shape(s) → …/pack.yaml
```

經 tenant-api 寫入時（`PUT /api/v1/tenants/{id}/custom-alerts`），同樣的宣告在寫入前就被拒：HTTP 400，錯誤碼 `INVALID_BODY`，`violations` 逐條列出被拒的宣告，什麼都不寫入。

## 背景

一次安全稽核確認了一類問題：租戶操作員（同租戶的一般寫入權，不需提權）宣告自訂告警時，若干未經驗證的租戶輸入能抵達生成的規則，進入 Prometheus 評估規則時的信任上下文。那個上下文能發起查詢，於是可以讀到**另一個租戶**的時序資料；輸入若讓規則檔無法載入，則規則更新被擋住、舊規則繼續評估（可用性風險）。稽核以發現者與驗證者分開的盲審確認；可直接利用的攔截點與繞過細節不寫進這份公開文件。

根治的做法是在評估期隔離：ruler 查詢時只看得到該租戶的資料。但這需要在 ruler 的查詢路徑上有個能注入租戶範圍的接點，而 Prometheus 本身的 ruler 在行程內直接評估，沒有這個接點（Thanos 的 Ruler 不支援多租戶，原因相同）。平台的 Prometheus 是單副本、上游原版映像。選擇因此只剩兩條：在 ruler 之外的編譯邊界攔下輸入，或換掉 ruler。

## 決策細節

### 在編譯邊界攔下輸入

保留共用 Prometheus 與向量化規則，攔截放在同步的寫入與產生路徑上，讓注入在規則產生時就不可能發生：

- **一份判斷，三個入口。** 驗證邏輯是 threshold-exporter 的 Go 程式（`components/threshold-exporter/app/pkg/config/custom_alert.go`），欄位的列舉、字元集與保留標籤集中在 recipe 契約檔 `components/threshold-exporter/app/pkg/config/recipe_contract.json`。tenant-api 寫入前呼叫它（`ValidateTenantCustomAlerts`，`PUT .../custom-alerts`、整份租戶設定寫入與批次寫入都經過）；exporter 載入設定時用它決定哪些宣告送出閾值序列，被拒的宣告不送、記一筆 ERROR log 並計入 `da_custom_alert_parse_errors`；編譯器（`da-guard compile-custom-alerts`，由 `compile_custom_alerts.py` 執行）只為 exporter 實際送出或已關閉的宣告產生規則。所以不經 tenant-api、直接改 conf.d 的宣告也走同一份判斷。
- **只放行平台寫的模板。** 編譯器在輸出前檢查每條規則的標籤與 annotation：除了 `{{ $value | printf "%.Nf" }}`、`{{ $labels.X }}`，以及讀取該告警所屬租戶 metadata 的固定模板之外，出現任何模板動作或反引號都直接失敗。
- **契約檔缺欄位時拒絕啟動。** recipe 契約檔若少了保留標籤或禁用模板字元的清單、或清單為空，解析失敗、程式無法啟動，不會退化成「什麼都接受」。
- **`promtool check rules` 是最後一道防線，不是主防線。** CI 對產生的 `rule-packs/rule-pack-custom-alerts.yaml` 跑 `promtool check rules`；主防線是上面的同步驗證，若只靠非同步的 CI，寫入、編譯、重新載入之間會有暴露窗。
- **selector 的值在同一份判斷裡先確認 Prometheus 載入得了。** 兩件事：值不含 PromQL 字串常值拒收的字元（無效 UTF-8 與 U+FFFD；tenant-api 收到的 JSON 裡任何無效位元組，解碼後都會變成 U+FFFD）；`selectors_re` 的值以 Prometheus 載入 `=~` 比對器的方式編得過（以 Perl 語法解析、再加頭尾錨點編譯）。不符合的宣告和其他無效欄位一樣被拒：tenant-api 回 400、exporter 不送、編譯器略過並警告。這道檢查存在的理由是可用性而非隔離：跳脫已保證值不會跳出字串，但只要規則包裡有一個 selector 載入失敗，Prometheus 的規則管理器會拒收這次所有規則檔、沿用上一份規則，之後的規則變更都上不去。

**平台功能的代價。** 這道限制沒有擋掉平台目前需要的任何東西，但不是「零」：平台自己的 annotation 現在就用了一個查詢函式，以固定模板讀取該告警所屬租戶的 metadata（`runbook_url`、`owner`、`tier`），它被允許是因為放行清單逐字比對這個模板。平台要在自訂告警的模板裡加入新的查詢，就得改放行清單與它的測試，經過程式審查。這是刻意接受的取捨。Prometheus 沒有關閉模板查詢函式的伺服器開關，「絕不讓未驗證的租戶輸入抵達能呼叫查詢的模板」是唯一做得到的形式。

**按叢集評估。** 一個租戶跑在多個叢集時（[ADR-006](006-tenant-mapping-topologies.md)），指標端的聚合保留叢集標籤 `da_cluster`，同一租戶兩個叢集的序列不合併、也不互相配對；閾值與維護旗標仍以租戶為單位配對。absence recipe 是例外，以整個租戶判斷。`da_cluster` 也是保留標籤，租戶不能用 selector 釘住它：釘住不會破壞隔離，但在各叢集自行評估規則的部署裡，序列不帶這個標籤，告警會靜靜地永遠不觸發。

### 評估期隔離與向量化，對共用規則互斥

向量化的做法是一條規則跑遍整個 TSDB，評估**之後**才以 `on(tenant) group_left` 依 `tenant` 標籤切分，租戶身份活在資料裡。評估期隔離正好相反：配對**之前**就移除別的租戶的序列，共用的配對退化成每個租戶自己對自己，規則必須逐租戶各評估一次，規則數又變回隨租戶數成長（ADR-024 §4 刻意消除的成本）。平台既有的規則包也大量依賴這種跨租戶配對（數百處 `on(tenant…)`）。

只把租戶自訂告警放進按租戶隔離的 ruler、平台規則包維持向量化，也逃不掉：那一部分仍需要另一個以 HTTP 查詢的 ruler，結構成本照付，只是套在較少的規則上。這種混合做法改變的是成本落在哪，不是有沒有成本。

### 不預先建評估期隔離，但方向先定

會需要評估期隔離的主要情境，是租戶能寫出超出固定 recipe 的查詢（原生 PromQL 或受限的查詢語言）。這項能力是否進 roadmap 不確定，所以不預先建。方向先定：若日後要做，與一次 ruler 遷移一起做；平台若改用獨立的 HTTP 查詢 ruler，就在它前面加 label proxy（平台已經有）；若改用 Mimir／Cortex，就用它們原生的按租戶評估。

## 重新評估的條件

任一成立就重新評估評估期隔離。第一條一旦成立影響最大，但會不會發生取決於不確定的 roadmap；第二、三條是平台自己看得到或控制得了的，較可能先發生。

| 條件 | 說明 |
|---|---|
| **租戶取得新的查詢表達力** | 租戶能寫出超出「固定 recipe + 已驗證參數」的查詢，邊界驗證就無法再保證「合法宣告 → 不跨租戶的 PromQL」。原生 PromQL 實質上需要評估期隔離；受限的查詢語言則未必，若編譯器能對語法樹做完整的安全改寫，可能仍能在編譯期守住。所以這條是「重新評估隔離問題」，不是直接改成評估期隔離。 |
| **現行模型內再出現同類缺陷** | 在現行的固定 recipe 模型內、已有編譯期驗證之下，又發現一個未驗證輸入抵達評估上下文。一次是 bug，兩次是模式，那時支持在 ruler 設結構性邊界。 |
| **平台因別的原因遷移告警後端** | 為了多叢集、ruler 高可用或長期儲存改用 Mimir／Cortex／Thanos Ruler 時，按租戶的接點或 HTTP 查詢路徑會隨之出現；向量化與隔離的取捨要在那次遷移中刻意重新決定，不事後補。 |
| **合規或客戶要求可證明的 ruler 層隔離** | 邊界驗證是緩解，不是隔離的證明；硬性的合規要求會翻轉成本效益。 |

**自帶 Prometheus／Thanos 的部署不在評估期隔離的範圍內。** 這類部署把同一批向量化規則包載入自己的 Thanos Ruler，經 Thanos Querier 看得到所有租戶（見 [BYO Prometheus 整合指南](../integration/byo-prometheus-integration.md)），一樣沒有按租戶的 ruler 隔離，一樣依賴編譯期驗證。上面任何評估期隔離的決定都不涵蓋它們。

## 後果與已知限制

- **變容易的**：跨租戶讀從「輸入穿透到規則」變成「在編譯邊界被攔下」；平台維持規則數隨形狀數成長的向量化，不需改架構。
- **新的租戶輸入不會自動受保護。** 這是邊界形狀的防護，只保護目前納入驗證的輸入。日後任何新的租戶可控輸入（多一個標籤欄位、多一個數值參數）只要進到規則產生，就要另外加驗證。受保護輸入的逐項清單留在驗證點的程式註解與 recipe 契約檔，不在這份公開文件列舉：公開列舉等於給出攻擊面地圖。
- **租戶可以指名任何指標。** metric 欄位只限字元集，不限於「屬於這個租戶的指標」；隔離來自配對：規則只對帶著該租戶 `tenant` 標籤的序列觸發。指標的 `tenant` 標籤由抓取設定加上，抓取設定標錯，這道隔離就跟著錯。
- **維護旗標以租戶為單位。** 一個租戶跑在多個叢集時，開啟維護會擋掉它在所有叢集的自訂告警。

由以下測試守住：

| 行為 | 測試 |
|---|---|
| 指標欄位夾帶 PromQL、selector 釘保留標籤（含 `tenant`、`da_cluster`）等宣告被拒 | `components/threshold-exporter/app/pkg/config/custom_alert_test.go` 的 `TestCustomAlert_ValidationNegatives` |
| 輸出的標籤與 annotation 只含平台模板 | `custom_alert_compile_test.go` 的 `TestAssertTemplateSafe` |
| 規則對租戶指標聚合或配對所依的標籤，都在保留標籤內 | `custom_alert_compile_test.go` 的 `TestCompiledAggregationKeysOnTenantMetricsAreReserved` |
| 各 recipe 按叢集聚合與配對 | `custom_alert_compile_test.go` 的 `TestCompiledRulesKeepTheClusterPerRecipe`、`TestSloCorePairsItsWindowsPerCluster` |
| selector 的值放行與否，與 Prometheus 解析器對產出 selector 的判定一致；載入不了的被三個入口拒收 | `custom_alert_selector_value_test.go` 的 `TestSelectorValues_MatchPrometheusParser`、`TestSelectorRegex_ExporterAndCompilerRefuse`；tenant-api 的 `TestPutCustomAlerts_UnloadableSelectorValueRejected400` |
| 契約檔缺保留標籤或禁用字元清單時拒絕啟動 | `recipe_contract_test.go` 的 `TestParseRecipeContract_FailsClosed` |
| tenant-api 對不合法宣告回 400 並列出 violations、不寫入 | `components/tenant-api/internal/handler/tenant_custom_alerts_test.go` 的 `TestPutCustomAlerts_InvalidRecipe400WithViolations` |

selector 值含模板字元時被拒（`rejectTemplateMetachars`），目前沒有直接的單元測試；輸出端的模板檢查會擋下同一類結果。

## 考慮過的替代方案

| 面向 | 換成按租戶評估的 ruler | ruler 查詢路徑加 label proxy | 編譯期邊界驗證（採用） |
|---|---|---|---|
| 從根關閉跨租戶讀 | 是（評估上下文收束） | 是（ruler 查詢強制加上租戶條件） | 不是根治，但移除了能注入的輸入 |
| 與向量化相容 | 否，規則數隨租戶數成長 | 否，需要逐租戶的規則 | 是，不變 |
| 須離開 Prometheus 本身 | 是（Mimir／Cortex 或按租戶分片） | 是（獨立的 HTTP 查詢 ruler） | 否 |
| 新元件與運維負擔 | 極高（換告警後端） | 高（獨立 ruler 加 proxy 接線，不換後端） | 低（驗證加一道 CI 檢查） |
| 對既有平台規則包的影響 | 高（共用評估普遍受影響） | 中到高（同一假設） | 無 |
| 修好已確認問題要多久 | 以季計 | 以季計 | 以日計 |

前兩者在向量化上代價相同，差在平台：換 ruler 等於換整個告警後端，label proxy 只多一個獨立 ruler、重用平台已有的 proxy，運維負擔低一階。

核心取捨：評估期隔離買到真正的隔離，也涵蓋未來可能重新引入查詢的 recipe 功能，代價是放棄平台最主要的擴充性質（高租戶數下規則數只隨形狀數成長）；編譯期驗證以近零成本、不動架構關閉已確認的注入輸入，弱點是通用性（每個新輸入要各自驗證）。問題是輸入注入而不是架構必然，編譯期驗證足以關閉它；評估期隔離的通用性，等相應功能真的要上線時再買。

## 相關

- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — 自訂告警與向量化編譯
- [ADR-006](006-tenant-mapping-topologies.md) — 一個租戶跑在多個叢集
- [ADR-031](031-slo-burn-rate-recipe.md) — SLO burn rate recipe，不擴大租戶查詢表達力
- [BYO Prometheus 整合指南](../integration/byo-prometheus-integration.md)
- [#1013](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1013) — 評估期隔離的完整架構評估（拓撲、各做法深入分析、整合成本）
