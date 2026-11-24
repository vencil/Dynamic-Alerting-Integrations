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
updated_at: 2026-11-24
---

# ADR-024: 宣告式 Dimensional 告警引擎 — Version-Aware Thresholds + Custom Alerts

> **Language / 語言：** **中文 (Current)** | [English](./024-version-aware-threshold-via-dimensional-label.en.md)

## 狀態

✅ **Accepted**（v2.9.0）。

2026-11-24 修訂：改寫為現行行為。決策不變。

- 2026-10-16：自訂告警每個租戶只取一處宣告，不做 `_defaults.yaml` 子樹繼承；一組租戶共用的政策寫在 profile（§3）。
- 2026-10-17：編譯器照 exporter 送出的清單編譯；每租戶上限由 tenant-api 寫入檢查與 da-guard 擋（§3、§4）。
- 2026-10-21：自訂告警的判定、recipe_id 與規則產生只有一份 Go 實作，recipe 的資料寫在一份契約檔（§9）。
- 2026-11-01：租戶 metadata（runbook_url、owner、tier）不再是告警 label，改由 annotation 讀出（§2）。
- 2026-11-02：自訂告警的閾值紀錄只在 exporter 各副本一致時才送出（見「後果與已知限制」）。
- v2.10.0：加入磁碟與 PVC 告警（§10）。

## 決策

平台用同一套「宣告式 dimensional」機制提供兩個能力，租戶兩者都只寫 YAML、不寫 PromQL：

- **版本感知閾值（Version-Aware Thresholds）**：租戶在閾值鍵上加 `{version="..."}`，告警依 app 目前實際跑的版本，比對該版本的閾值；沒宣告的版本沿用不帶版本的閾值。
- **自訂告警（Custom Alerts）**：標準 rule pack 涵蓋不到的告警，租戶從平台寫好的 recipe（參數化的告警樣板）選一種、填參數，平台把它編成規則。

兩者共用的機制是：threshold-exporter 把租戶的值送成帶維度標籤的 `user_threshold` 序列；規則以「精確版本優先、沒有就退回 `default`」的方式 join 閾值，並依嚴重度拆成各自的規則；維護模式以 `user_state_filter` 排除；租戶之間以 `tenant` 標籤隔離。

### 範例一：版本感知閾值

租戶設定（v2 收緊到 60，其他版本沿用 70）：

```yaml
tenants:
  db-a:
    container_cpu: "70"
    container_cpu{version="v2"}: "60"
```

threshold-exporter 的 `/metrics` 輸出：

```text
user_threshold{component="container",metric="cpu",severity="warning",tenant="db-a"} 70
user_threshold{component="container",metric="cpu",severity="warning",tenant="db-a",version="v2"} 60
```

kubernetes rule pack 把不帶 `version` 的那一列補成 `version="default"`，再拿 Pod 的 CPU 使用率（帶 Pod 的 `app.kubernetes.io/version`）去比：跑 v2 的 Pod 比 60，跑 v1、v3 或沒有版本標籤的 Pod 比 70。觸發的 `PodContainerHighCPU` 帶 Pod 實際的 `version`。規則細節見 §2。

### 範例二：自訂告警

租戶設定（開發範例 conf.d 裡的 `db-b`，節錄）：

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

threshold-exporter 送出的閾值序列：

```text
user_threshold{component="custom",metric="mysql_global_status_threads_connected",mode="page",name="mariadb_conns_high",recipe_id="threshold__mysql_global_status_threads_connected__gt__w5m__for1m",severity="warning",tenant="db-b"} 150
```

`make custom-alerts-compile` 從同一份 conf.d 產生的告警規則（節錄）：

```yaml {.quote src="rule-packs/rule-pack-custom-alerts.yaml"}
  - alert: Custom_threshold__mysql_global_status_threads_connected__gt__w5m__for1m
    expr: custom:threshold__mysql_global_status_threads_connected__gt__w5m__for1m:warning:core
    for: 1m
    labels:
      severity: warning
      maintenance: mute
      tenant: '{{ $labels.tenant }}'
      recipe: threshold
      component: custom
      mode: '{{ $labels.mode }}'
```

`recipe_id` 是這條告警的 shape（recipe、metric、運算子、窗口、`for` 組成的識別）：宣告同一個 shape 的租戶共用這一條規則，每個租戶的閾值由 `user_threshold` 的那一列提供。

以下十條是這套機制的組成，每條附取捨。

### 1. 多版本閾值用 `version` 維度標籤表達，不新增 schema

版本切換是自然發生的：Pod 升版後，它的指標帶上新的 `version`，join 就對上該版本的閾值；滾動升級期間新舊 Pod 並存，各自比各自版本的閾值。租戶不需要在升版那一刻改設定。

`version` 與既有的 `env`、`tablespace_re` 等維度走同一條路：exporter 照閾值鍵上的標籤送出序列，parser 與送出邏輯沒有為它新增程式。哪些閾值可以帶哪些維度，由門檻登錄表（`rule-packs/threshold-registry.yaml`）的 `dimensions` 決定，產生到根 `_defaults.yaml` 的 `dimensions:`；目前只有 `container_cpu` 與 `container_memory` 宣告了 `version`。其他閾值寫 `{version="..."}` 不會送出，共用鍵判決（`da-guard key-verdict`）把它讀成 `unread`（`no_row`），tenant-api 寫入時拒收。字面上的 `version="default"` 保留給退回分支，tenant-api 寫入時也拒收。

*取捨*：不新增 `versioned:` 之類的 YAML 區塊，避免改動每個租戶 hot-reload 都會走的設定 parser；代價是同一個閾值的多個版本散在幾個相鄰的鍵。

### 2. Rule pack 的處理：注入版本、退回 default、依嚴重度拆規則

cAdvisor 的 CPU 指標本身不帶版本。kubernetes rule pack 在 recording rule 裡 join kube-state-metrics 的 `kube_pod_labels`，把 Pod 的 `app.kubernetes.io/version` 標籤帶成 `version`，每一層 `by(...)` 聚合都保留它。接著閾值與指標兩邊都把空的 `version` 補成 `default`：

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
      - record: tenant_version:alert_threshold:container_cpu
        expr: |
          label_replace(
            max by(tenant, version, severity) (
              user_threshold{component="container", metric="cpu"}
            ),
            "version", "default", "version", "^$"
          )
```

每個嚴重度一條核心規則，結構是「精確命中，或退回 default」：

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
      - record: rule_pack_kubernetes:pod_container_high_cpu_warning:core
        expr: |
          (
            (
              tenant_version:pod_weakest_cpu_percent:vlabeled
              > on(tenant, version) group_left()
                tenant_version:alert_threshold:container_cpu{severity="warning"}
            )
            or
            (
              (
                tenant_version:pod_weakest_cpu_percent:vlabeled
                unless on(tenant, version)
                  tenant_version:alert_threshold:container_cpu{severity="warning"}
              )
              > on(tenant) group_left()
                tenant_version:alert_threshold:container_cpu{version="default", severity="warning"}
            )
          )
```

告警規則再在核心規則後面加上 `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)`。三個刻意的設計：

- **退回 default**：租戶只在某個版本需要不同閾值時才宣告；沒宣告的版本自動比 default，不會出現「新版上線卻沒有閾值在看」的空窗。`group_left()` 保留指標實際的版本，所以退回時告警仍標出是哪個版本。
- **依嚴重度拆規則**：固定 severity 後，右邊每個 (tenant, version) 只有一列，精確分支是一對一、退回分支是多對一。若改用單一規則加 `group_left(severity)`，`version × severity` 會讓 join 出現一對多或多對多而整條規則評估失敗。
- **維護排除不帶 version**：閾值比對用 `on(tenant, version)`，維護旗標沒有 version，以 `unless on(tenant)` 排除，兩者不衝突。

**metadata 走 annotation，不進告警 label。** threshold-exporter 為每個租戶送出 `tenant_metadata_info{tenant, runbook_url, owner, tier}`（值恆為 1）。規則不 join 這個指標；要顯示 metadata 的 annotation 在告警評估時以同一個查詢樣板讀出（`PodContainerHighCPU` 的 annotation，節錄）：

```yaml {.quote src="rule-packs/rule-pack-kubernetes.yaml"}
          runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
```

`topk(1, …)` 在多個副本各送一份時只取一筆；租戶沒有 metadata 時 annotation 是空字串，告警照常觸發。不 join 的理由：threshold-exporter 跑多個副本、各自 reload，租戶改 metadata 後的一兩分鐘內兩個副本送出的值不同；join 會讓告警 label 跟著挑到的那一筆變（VictoriaMetrics 的 `topk` 遇到同值序列每次可能挑不同的一筆），告警身分一變 `for:` 就重新計時，該租戶在這段期間不觸發。放在 annotation，空窗內頂多顯示其中一個副本的值（見 [HA 設計](../design/high-availability.md) 的 4.3 節）。查詢樣板的寫法由 `check_leftouterjoin_enrichment.py` 鎖定，它也擋下任何 join `tenant_metadata_info` 的運算式。

**部署前提：kube-state-metrics 必須設 `--metric-labels-allowlist=pods=[app.kubernetes.io/version]`。** 沒設的話 `kube_pod_labels` 不帶版本，所有 Pod 都退回 `default`，版本閾值形同不存在，而且不會報錯。三道防護：

- 執行期告警 `VersionAwareThresholdInert`：宣告了版本閾值、租戶 Pod 在跑，但該叢集的 kube-state-metrics 完全沒送 Pod 標籤，10 分鐘後觸發。
- CI 檢查 `check_ksm_version_allowlist.py`：repo 內每個 kube-state-metrics Deployment 的 allowlist 都要含 `app.kubernetes.io/version`。執行期告警看不到「allowlist 設成別的標籤」的情況，由它補。
- repo 自帶的 kube-state-metrics 部署（`k8s/03-monitoring/deployment-kube-state-metrics.yaml`）已設好這個參數。

**範圍**：版本感知目前只用在 `container_cpu` 與 `container_memory`。`container_cpu_throttle` 的規則已是同樣的精確或退回結構，但它沒有宣告 `version` 維度，exporter 不送它的版本閾值，所以一律走 default。要開放它不必改規則：在門檻登錄表為 `container_cpu_throttle` 加上 `dimensions: [version]`、跑 `python3 scripts/tools/lint/check_threshold_registry.py --regen` 重新產生出貨的 `dimensions:`，並刪掉 `tests/shared/test_dimension_registry.py` 裡 `_KEPT_BUT_NOT_SERVED` 的對應例外。

### 3. 自訂告警：平台寫好的參數化 recipe，租戶不寫 PromQL

跨租戶隔離已由既有架構處理：`tenant` 標籤在抓取時由 namespace 決定，租戶偽造不了。所以設計問題是表達力與規則數成本。

- **recipe 庫**：平台寫好 7 種 recipe：`threshold`、`rate`、`ratio`、`absence`、`p99_latency`、`forecast`、`slo_burn_rate`（最後一種見 [ADR-031](031-slo-burn-rate-recipe.md)）。租戶或平台填 metric、窗口、運算子、閾值與嚴重度、`mode` 等參數，存成宣告式 YAML。*取捨*：表達力受 recipe 庫限制，換來「合法的參數一定產生合法的 PromQL」。

- **每個租戶只取一處宣告**：exporter 依序找租戶自己的 `_custom_alerts`、根目錄平台檔（例如 `_platform.yaml`）`tenants:` 底下該租戶的條目、租戶綁定的 profile，取到第一處就停，不合併。任何一層 `_defaults.yaml` 裡的 `_custom_alerts`、根目錄平台檔頂層的 `_custom_alerts` 都不會送出：不做子樹繼承。要讓一組租戶共用同一條政策，寫在 profile、讓這些租戶綁定它；租戶寫了自己的 `_custom_alerts` 就整個取代 profile 的清單，也可以改綁或解除綁定。

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

  `da-guard served-values` 的結果：`pay-a` 送出 `payment_failure_ratio`，`pay-b` 送出 `pending_settlements_high`；`refund_ratio` 兩個租戶都沒有。

- **編譯器編的就是 exporter 送出的清單**：`da-guard compile-custom-alerts` 取 exporter 為每個租戶取得的 `_custom_alerts`（與 `da-guard served-values` 是同一段 Go 程式）產生規則，不另外走一次 conf.d。條目合不合法、與同租戶前面的條目是否重複、它的 recipe_id 與 PromQL，都由這同一份 Go 程式決定（§9），所以不會有「exporter 送出了閾值、編譯器卻沒有對應規則」的條目。exporter 拒收的條目，編譯器不編、印一行警告，其餘照常編譯。`compile_custom_alerts.py` 只負責讀寫檔案、`--check` 比對與輸出序列化。

- **metric 自動完成**：tenant-api 的 `GET /api/v1/tenants/{id}/metrics` 向 Prometheus 查最近 24 小時內帶 `{tenant="<id>"}` 的指標名稱（可用 `?q=` 篩前綴），給 portal 的 recipe 表單當選項。它是無狀態的代理，每次都重查，不另存一份會與 Prometheus 不一致的清單；matcher 由後端組出，租戶輸入碰不到它的結構；流量由 tenant-api 既有的每呼叫者限流擋（`TA_RATE_LIMIT_PER_MIN`，預設每分鐘 100 次）。租戶可以先為之後才會出現的指標建 recipe，所以後端不擋「指標目前不存在」。

- **寫入由後端負責**：portal 的 recipe 表單只產出 JSON；寫入經 tenant-api 的 `PUT /api/v1/tenants/{id}/custom-alerts`，由後端改 YAML（保留註解與縮排），並以整檔 hash 做樂觀並行控制（`base_hash` 與現況不符回 409）。

### 4. 向量化編譯：規則數隨 shape 數成長，三道護欄

編譯器為每個 shape 產生一組向量化規則：一條「指標 `> on(tenant, version) group_left(name, mode)` 該 shape 的閾值」涵蓋所有宣告這個 shape 的租戶，所以規則數等於 shape 數，不是租戶數。

但這只對「共用同一個指標」成立。租戶 A 的 `order_created_total` 與租戶 B 的 `payment_failed_total` 一定是兩條規則，所以自訂告警的規則數隨自訂告警總數線性成長，不享有 rule pack 在 [benchmarks.md](../benchmarks.md) 量到的規則數不隨租戶數成長的特性。護欄：

1. **每租戶上限**：每個租戶最多 20 列自訂告警閾值（契約檔的 `custom_alert_cap`；`slo_burn_rate` 一條算兩列，停用的不算）。tenant-api 寫入時拒收，da-guard 以 `custom_alert_over_cap` 在 CI 擋，validate-config 在 `guard_errors` 列出。
2. **全域規則數預算**：還沒做。數值要以實測的規則評估時間反推。
3. **promtool**：CI 以 promtool 對各 recipe 編出的規則跑觸發與不觸發的測試（`tests/dx/test_custom_alerts_promtool.py`），`make custom-alerts-compile-check` 確認 commit 的規則包與 conf.d 一致。

**編譯的來源必須是 exporter 實際載入的 conf.d。** 不同的話，規則的 shape 對不上 exporter 送出的 `user_threshold`，規則永遠不觸發，也不會報錯。

### 5. 驗證雙層：同一份 Go 程式，寫入時看單一租戶、CI 看整棵樹

- **寫入時**：tenant-api 收到 `PUT` 就在程序內驗這個租戶的 `_custom_alerts`，無效回 400 並列出違規，壞的輸入進不了 repo。它呼叫的是 exporter 那份判定程式，檢查 metric 與標籤格式、保留標籤、recipe／op／severity／mode／`for`／horizon 列舉、forecast 比例下限、非有限數，以及每租戶上限。
- **CI**：`da-guard compile-custom-alerts` 對整棵樹產生規則，再加上 §4 的 promtool 測試。它判定條目用的是同一份程式，兩層的差別只在看到的範圍。

*取捨*：正式環境的 tenant-api 與 exporter 映像不帶 promtool。租戶不寫 PromQL、模板由平台寫，合法的參數就產生合法的 PromQL；promtool 能多抓到的只有模板本身的錯，那是平台的問題，留給 CI。需要整棵樹的檢查（例如 shape 數與規則數）也只在 CI 做：tenant-api 本機的樹在 GitOps 同步前不是最新的，拿它判斷會誤放。

### 6. 靜默與路由隔離：沿用 sentinel 與 inhibit

自訂告警的 `mode`（`page` 或 `silent`）與 `name` 由 `group_left(name, mode)` 帶到告警上。Alertmanager 這樣處理：

- **`silent` 用 sentinel 加 inhibit，不路由到空 receiver。** 這沿用 [ADR-003](003-sentinel-alert-pattern.md) 的作法：sentinel 是一條專門用來抑制別的告警的告警，inhibit 是 Alertmanager「來源告警存在時壓下目標告警通知」的規則。編譯器產生一條 `CustomRecipeSilent`（`severity: none`，每個 `mode="silent"` 的 (tenant, name) 一條），inhibit 以 `equal: ["tenant", "name"]` 只壓下那一條自訂告警的通知，同一個 shape 裡 `page` 的告警照常通知。被壓下的告警仍是 Prometheus 的 `ALERTS` 序列，所以 `silent` 等於只在儀表板上看得到。`CustomRecipeSilent` 本身帶 `component: sentinel`，由 sentinel 專用的 sinkhole 路由（`continue: false`）收走，不會變成通知。
- **路由隔離**：每條自訂告警帶 `component: custom`。Alertmanager 的 `component="custom"` 路由排在平台預設路由之前、`continue: false`，`group_by: ["tenant", "alertname"]`，租戶的告警風暴不會灌進平台值班的管道。有設 `_routing` 的租戶，路由產生器在這條路由底下為它產生子路由、送到它自己的 receiver；沒設的落到 `custom-alerts-firehose`，這個 receiver 沒有通知器，告警只在 Alertmanager UI 與 `ALERTS` 序列上看得到。

### 7. Forecast recipe：預測趨勢多久後耗盡

`forecast` 回答「磁碟或記憶體照目前趨勢，N 小時內會不會耗盡」。直接用 `predict_linear` 容易誤報（瞬間尖峰被線性外推），所以由平台把誤報來源擋掉，包成 recipe。

- **兩種模式**：有 `capacity_metric` 時是比例模式，預測「可用量 / 容量」會不會低於閾值；沒有時是原始值模式，預測 gauge 會不會越過絕對閾值。
- **回看長度由平台推導**：`lookback = max(2 × horizon, 1h)`，以整數秒寫進規則；租戶只選 `horizon`（`1h`、`2h`、`4h`、`12h`、`24h`、`48h`）。回看長度是最容易設錯的旋鈕，不開放給租戶。
- **冷啟動門檻**：回看範圍內樣本數要大於 3 才評估，避免剛部署時樣本太少造成斜率亂跳。只適用 gauge，counter 要先用 `rate`。
- **比例模式的現狀門檻**：除了預測值低於閾值，還要求目前的可用比例已低於 0.5，把「預測會低」收斂成「預測會低、而且現在已經偏低」，擋掉容量充足的磁碟上的寫入突波。預測值以 `clamp_min(…, 0)` 夾在 0 以上。因此比例模式的閾值必須在 (0, 0.5) 之間：tenant-api 寫入時拒收；exporter 不送出越界的條目，編譯器跳過它並印警告，其餘照常編譯。

租戶宣告：

```yaml
- recipe: forecast
  name: disk_will_fill
  metric: kubelet_volume_stats_available_bytes
  capacity_metric: kubelet_volume_stats_capacity_bytes
  op: "<"
  horizon: 4h
  threshold: "0.15:warning"   # 預測 4h 內可用比例掉破 15%
```

編出的預測規則以 `predict_linear(…[28800s], 14400)` 計算（回看 8 小時、預測 4 小時），並帶 `< 0.5` 與 `count_over_time(…[28800s]) > 3` 兩個條件。

**`for` 是 shape 的一部分。** `for`（條件持續多久才觸發）是規則的靜態屬性，無法像 `mode` 一樣由資料帶進來；若不放進 shape，兩個租戶共用 shape 而 `for` 不同時，其中一個的 `for` 會被忽略。所以 `for` 進 recipe_id（例如 `__for1m`），並限定為 `0s`、`1m`、`5m`、`15m`、`30m`、`1h`，預設 `1m`，讓 shape 數不會因任意值而膨脹。

### 8. Recipe 生命週期：active、deprecated、eol

recipe 是平台寫的，它的 `status` 決定租戶能不能繼續用。這是 recipe 本身的版本治理，與 §1 的 app `version` 標籤無關。狀態寫在 recipe 契約檔（§9）。

- **active**：正常使用。
- **deprecated**：照常編譯，編譯器印非致命警告，portal 在選項上標示；仍可新增。
- **eol**：既有宣告照常編譯，平台退役一個 recipe 不會刪掉已部署租戶的告警；但寫入端擋住「擴張」。

**eol 只擋擴張。** 對每個 eol recipe，一次寫入裡使用它的條目數不得超過檔案現況：擋新增、加量、換成另一個 eol recipe，放行改參數、改名、刪除與原樣保留。刻意不擋「任何含 eol recipe 的寫入」：那樣會讓一個與事故無關、兩年前留下的 eol recipe，擋住租戶在事故中新增救急告警的整次寫入。portal 的表單也不讓新告警選 eol recipe。

**為什麼在寫入端擋**：判斷擴張要比對「現況」與「將寫入」，只有 tenant-api 的共用寫入檢查同時看得到兩者，所以所有寫入端點（整租戶 `PUT`、`custom-alerts` 的 `PUT`、批次）都經過它。CI 的編譯器看得到整棵樹，卻分不出哪些是新宣告，所以只能警告。

**用量可見**：編譯器為每個使用中的 shape 產生一條 `custom_recipe_info{recipe_id, recipe, status}`，SRE 可以把用量接上狀態，畫出技術債的消化進度：

```promql
count by(recipe_id)(count by(recipe_id, tenant)(user_threshold{component="custom"}))
  * on(recipe_id) group_left(recipe, status) custom_recipe_info
```

內層先 `count by(recipe_id, tenant)` 去掉重複的租戶：`recipe_id` 不含 severity，直接 `count` 會把同一租戶的 warning 與 critical 算成兩個。退役本身仍由 SRE 手動進行。

**eol 不是緊急下線。** 一個 recipe 若有害、必須立即停用，作法是從 recipe 庫刪掉它的定義：exporter 把使用它的條目當成未知 recipe 拒收，不再送出閾值，編譯器也不再為它產生規則（印警告）；tenant-api 寫入時同樣拒收。

### 9. Recipe 規則只有一份：Go 實作與 recipe 契約檔

一條自訂告警從寫入到產生規則，會經過 tenant-api 的寫入檢查、exporter 送出閾值、CI 編譯規則、recipe-preview 預覽。這幾處判斷「合不合法、算哪個 shape、產生什麼 PromQL」都用同一份 Go 程式（`components/threshold-exporter/app/pkg/config/`）：

- tenant-api 與 exporter 直接呼叫它；
- CI 編譯呼叫 `da-guard compile-custom-alerts`，`compile_custom_alerts.py` 只負責讀寫檔案（寫入一定要指定 `--out`；結果為空時不覆蓋已有規則的規則包，除非加 `--allow-empty`）、`--check` 比對與輸出序列化；
- recipe-preview（以近期資料試算一條 recipe 會不會觸發的服務）的映像帶 da-guard，預覽與部署用同一份判定。

portal 的 recipe 表單在瀏覽器執行、不跑 Go：它只做即時提示，界限讀下表的產生物，合不合法以 tenant-api 的寫入檢查為準。

規則裡的**資料**寫在一份 recipe 契約檔 `recipe_contract.json`，與上述 Go 程式同一個 package，以 `go:embed`（Go 在編譯時把檔案內容嵌進執行檔的機制）嵌入：各 recipe 可帶、必填、不收的欄位與固定運算子，各欄位的列舉與預設值，metric、label、window、name 的格式，不可當 selector 的保留標籤，p99_latency 的 quantile 範圍，forecast 的現狀門檻、比例下限與回看推導，slo_burn_rate 的偵測窗口與燒錯倍數，生命週期狀態，每租戶上限。exporter、da-guard 與 tenant-api 經這個 package 直接讀它；其他地方的副本都由 `scripts/tools/dx/gen_recipe_contract.py` 產生，`--check` 比對到差異就紅：

| 產生物 | 用途 |
|---|---|
| `docs/schemas/tenant-config.schema.json` 的自訂告警條目定義 | 編輯器提示、validate-config 的 schema 檢查 |
| portal 的 `recipe-enums.json`、`recipe-status.json` | recipe 表單的選項與狀態標示 |
| `rule-packs/recipes/*.yaml` 的狀態、必填與否、列舉與預設值 | 給人讀的治理文件 |
| `.github/custom-rule-policy.yaml` 與 `docs/custom-rule-governance.md` 的 forecast 最長回看 | 自訂規則 lint 的查詢範圍上限 |

為什麼不用多份實作加比對測試：同一條規則寫成多種語言，比對測試只能抽樣，抽不到的地方會各自演變。Go 與 Python 兩份實作就曾這樣分歧：Go 不准租戶以 `mode` 當 selector，Python 的保留標籤清單沒有它。只留一份實作，這類差異就沒有地方發生。[Sloth](https://github.com/slok/sloth)、[Pyrra](https://github.com/pyrra-dev/pyrra)（兩者都從 SLO 宣告產生 Prometheus 規則）也是單一 Go 程式，驗證與產生規則共用同一份程式碼。

### 10. 磁碟與 PVC 告警（Addendum）

租戶可以對磁碟容量設告警，沿用既有的 recipe（`forecast`、`ratio`、`threshold`），不新增種類。和一般 recipe 不同的有四點。

**前提：PVC 由支援 volume stats 的 CSI driver 提供。** kubelet 只對實作了 `NodeGetVolumeStats` 的儲存後端送出 `kubelet_volume_stats_*`。平台的抓取設定從 kubelet 收 `kubelet_volume_stats_*`，把 `namespace` 直接當成 `tenant`（一個 namespace 對一個租戶，見 [ADR-006](006-tenant-mapping-topologies.md)）。

**逐顆 PVC 判斷，不要被加總掩蓋。** 一個租戶可能掛好幾顆 PVC：一顆 10GB 的設定碟已經 99% 滿、一顆 500GB 的資料碟才 10% 滿，`by(tenant)` 加總後整體還有大量空間，告警永遠不觸發。所以 recipe 可以寫 `group_by: [persistentvolumeclaim]`：聚合時保留這個維度，閾值仍是每租戶一個、套到該租戶的每顆 PVC，任一顆達標就觸發，告警標出是哪一顆。為了控制序列數，`group_by` 只能寫 `persistentvolumeclaim`，`absence` 與 `slo_burn_rate` 不支援。

**設錯的告警要大聲壞掉。** 對沒被抓取、或沒有歸屬到 `tenant` 的指標設告警，規則能編譯、卻永遠不觸發。兩道防護：

- **CI**：抓取可達性檢查（`check_scrape_reachability.py`）解析 Prometheus 的抓取設定，rule pack（含自訂告警規則包）用到的每個葉指標都要被至少一個抓取工作收進來。它只看得到本 repo 的抓取設定。編譯器編到磁碟 recipe 時也印一行提示，提醒確認叢集符合上述前提。
- **執行期告警 `CustomRecipeDiskInert`**：租戶宣告了磁碟 recipe、Pod 也在跑，但該叢集看不到這個租戶的 volume stats，15 分鐘後觸發。它送給宣告的租戶，因為最常見的原因是租戶的工作負載根本沒掛 PVC；`platform_summary` 另外提示 SRE 檢查抓取歸屬與 CSI driver。

## 背景

租戶需要：(1) 規則可以先 commit 進 `conf.d/`、不立即生效；(2) 生效時機與 app 升版對齊；(3) 查問題時答得出「現在跑的是哪個版本、比的是哪個閾值」；(4) YAML 不累積已經沒有意義的生效日期。另一方面，標準 rule pack 涵蓋不到各領域與 app 層的告警需求，租戶需要能自訂，但不能因此被迫寫 PromQL。

設計受三條既有約束：

- **只宣告**：平台寫 rule pack 的 PromQL，租戶只在 YAML 填數字或參數。要租戶寫 PromQL 的方案都違反這條（[ADR-008](008-operator-native-integration-path.md)）。
- **`user_threshold` 本來就帶維度**：`user_threshold{tenant, component, metric, severity, <維度標籤>}` 已支援 `env`、`tablespace_re` 等維度。
- **複雜度留在平台的 rule pack**，不下放給租戶；每租戶的序列數上限既已存在（`max_metrics_per_tenant`，超過就截斷）。

兩個能力共用同一套機制、同一個 tenant-api 寫入檢查、同一條 CI 流程，所以寫在同一份 ADR。

### 與排程式閾值的分工

[設定驅動設計](../design/config-driven.md) 2.6 節的排程式閾值（`overrides: [{window, value}]`）與本 ADR 是兩個獨立機制，可以同時用在同一個租戶：

| | 排程式閾值 | 版本感知閾值 |
|---|---|---|
| 切換依據 | 時間（每天重複的 UTC 時段） | 指標上的 `version` 標籤，不看時間 |
| 觸發 | 時鐘到點，每天重複 | app 升版後指標帶新版本，一次性切換 |
| 典型場景 | 夜間 22:00–06:00 放寬到 200 | v2 上線後 CPU 閾值由 80 收緊到 60 |

## 後果與已知限制

**變容易的**：閾值切換自動跟著 Kubernetes 滾動升級，沒有時間差；對帶 `version` 的指標，「現在跑哪些版本」可以用 `count by(version)(<指標>)` 直接查；YAML 不再累積過期的生效日期；租戶自訂告警不需要平台逐條加規則。

**要承擔的**：

- **打錯版本號會靜默退回 default。** 退回機制讓沒宣告的版本有閾值可比，代價是 `{version="v2 "}` 這類筆誤不會報錯，只會比到 default。
- **序列數截斷要固定。** 租戶超過 `max_metrics_per_tenant` 時，exporter 截斷前先依固定順序排序：不帶版本與 `version="default"` 的閾值排前面、一定保留，帶版本的依標籤字典序從尾端截。否則 Go map 的隨機順序會讓每次抓取截掉不同的版本，告警忽隱忽現、重複發通知。
- **儀表板與 portal 的查詢要考慮 `version`。** 租戶開始寫版本鍵後，沒有聚合的查詢會多出帶 `version` 的序列。
- **告警不帶 runbook_url、owner、tier label。** metadata 改由 annotation 查詢（§2），依這三個 label 路由或抑制的 Alertmanager 設定要改用 `tenant`；每個顯示 metadata 的 annotation 在每次評估多一次查詢。
- **換版空窗內，正在改 mode 或 name 的那一條自訂告警暫停。** 自訂告警的 `name`、`mode` 仍是 label（`mode` 決定路由），所以閾值紀錄只在各副本對同一個 (tenant, version, severity) 送出相同的 name 與 mode 時才產生（`and on(tenant, version, severity) (count by(…)(…) == 1)`，不挑其中一筆）。租戶正在改某條自訂告警的 mode 或 name 時，那一條在 Prometheus 與 VictoriaMetrics 上都暫停，其他告警不受影響（自訂閾值不帶 `version`，一個鍵就是一條告警的一個嚴重度）。副本都正常 reload 時，暫停約一兩分鐘；若有副本卡在舊設定，那條告警會一直暫停，平台告警 `CustomAlertReplicasDisagree` 在不一致持續 10 分鐘後觸發（見 [VictoriaMetrics 整合](../integration/victoriametrics-integration.md) 的 3.1 節）。
- **診斷工具只看 exporter 送出的清單。** `describe_tenant`（列出一個租戶最終設定的 CLI）顯示 exporter 為租戶送出的每條自訂告警與它的狀態，不顯示把子目錄 `_defaults.yaml` 與租戶自己的清單合起來的視圖，因為 exporter 從不送出那種合併結果。「這次 conf.d 變更影響什麼」由 config-diff 回答（[ADR-037](037-config-diff-compares-rendered-output.md)）。
- **recipe-preview 映像多帶一支 da-guard。** 預覽與部署用同一份判定，映像因此變大（da-guard 是單一靜態 Go 執行檔）。
- **全域規則數預算還沒做**（§4）。

## 考慮過的替代方案

- **`ScheduledValue.from/until` 絕對日期**：YAML 會累積已經沒有意義的生效日期（過期的 `from` 永遠成立），還要同時寫兩處、有一致性風險。把時間軸塞進宣告式設定是結構性的錯。
- **`POST /active-version` 寫入目前版本的 API**：多出第二個狀態來源，破壞單一事實來源；滾動升級中途呼叫會造成短暫不一致。指標本身帶的版本就是事實來源。
- **排程合併 PR**：Git 合併是瞬間的二元切換，對不上 Kubernetes 5 到 10 分鐘的漸進發佈與 GitOps 傳遞延遲；helm rollback 也不會反向 revert PR。
- **以 `or on() vector(0)` 補值**：破壞下游聚合，對「值為 0 就告警」的規則造成誤報。正解是 `label_replace(..., "version", "default", "version", "^$")`。
- **租戶直接寫 PromQL**：違反只宣告的約束。
- **`silent` 路由到空 receiver**：與 [ADR-003](003-sentinel-alert-pattern.md) 的 inhibit 作法不一致，Alertmanager UI 也看不出抑制狀態。
- **讓 exporter 對 `_defaults.yaml` 子樹做聯集繼承**：能保住「寫一次、套整棵子樹」，但寫在 `_defaults.yaml` 的 recipe 會一次對整棵子樹生效（例如 absence recipe 對所有沒送該指標的租戶同時觸發），而租戶要怎麼選擇不套用沒有定義。
- **編譯器另外實作一份與 exporter 相同的來源規則**：改動最小、編譯環境不需要 da-guard，但兩份實作仍會各自演變。
- **保留 Python 編譯器，以 Go 與 Python 的比對測試對齊**：比對只能抽樣，抽不到的差異照樣漂移（§9 的 `mode` 就不在抽樣範圍內）；每加一條規則要在兩種語言各寫一次。
- **契約檔改用 JSON Schema 表達，由 Go 讀 schema**：Go module 沒有 JSON Schema 函式庫，要嘛新增依賴、要嘛手抄一份；schema 本來是被程式碼釘住的一方，改由它驅動程式等於把方向倒過來（與 [ADR-038](038-unread-keys-judged-by-readers.md) 否決「讓 schema 當登錄表」同理）。
- **以 join 把 metadata 帶進告警 label、右邊收斂成每租戶一筆**：告警可以直接依 owner、tier 路由，但換版空窗內收斂挑到的那筆會在副本之間跳動，告警身分跟著變、`for:` 重來。metadata 是會變的描述資訊，不該是身分。
- **把 `_custom_alerts` 從 list 改成以名稱為 key 的 dict**：list 形態貫穿 exporter、tenant-api、portal 與編譯器，改形態的影響範圍不成比例；每個租戶只取一處來源，也不需要依名稱合併。

## 相關

- [ADR-003: Sentinel Alert 模式](003-sentinel-alert-pattern.md) — 靜默所用的 sentinel 與 inhibit
- [ADR-006: 租戶映射拓撲](006-tenant-mapping-topologies.md) — 基礎設施指標的租戶歸屬
- [ADR-008: Operator-Native 整合路徑](008-operator-native-integration-path.md) — 只宣告的約束
- [ADR-017: `_defaults.yaml` 繼承](017-defaults-yaml-inheritance-dual-hash.md) — 閾值的目錄樹繼承（`_custom_alerts` 不走這條，見 §3）
- [ADR-020: Tenant Federation](020-tenant-federation.md) — 跨租戶讀取路徑的隔離
- [ADR-031: SLO burn-rate recipe](031-slo-burn-rate-recipe.md) — 第 7 種 recipe
- [ADR-037: config-diff 比較渲染結果](037-config-diff-compares-rendered-output.md) — conf.d 變更的影響範圍
- [Version-Aware Thresholds 使用攻略](../scenarios/version-aware-thresholds.md) — 租戶宣告與 kube-state-metrics 設定的操作步驟
- [Recipe 預覽設計](../design/recipe-would-fire-preview.md) — recipe-preview 服務
- [#423](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/423) — 版本感知閾值
- [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) — 自訂告警
- [#692](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/692) — 磁碟與 PVC 告警
