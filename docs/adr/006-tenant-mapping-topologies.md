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
updated_at: 2026-11-24
---
# ADR-006: 租戶映射拓撲 (1:1, N:1, 1:N)

> **Language / 語言：** **中文 (Current)** | [English](./006-tenant-mapping-topologies.en.md)

## 狀態

✅ **Accepted**（v2.1.0）。

2026-06-14 修訂：磁碟、PVC、節點這類基礎設施指標只支援 1:1 歸屬（見「基礎設施指標的租戶歸屬（Addendum）」）。

2026-11-21 修訂：同一個租戶跑在多個叢集時，以每台 Prometheus 的 external label `da_cluster` 標出叢集，規則與告警逐叢集計算（見「多叢集同租戶」）。

2026-11-22 修訂：通知的分組預設不按叢集拆；租戶可以逐叢集宣告預期的 exporter（`種類@叢集`）；Kubernetes 的兩項「設定了卻沒有對應資料」檢查逐叢集判定。

2026-11-23 修訂：租戶自助告警（`Custom_*`）逐叢集判定，只有 `absence` recipe 維持整個租戶；`da-tools byo-check prometheus` 與 `da-tools onboard` 也指出沒設 `da_cluster` 的 Prometheus。

2026-11-24 修訂：改寫為現行行為。決策不變。補上兩項限制：產生的 1:N 映射規則不在平台附的 Prometheus 部署裡，平台的規則包也不讀映射後的序列。

## 決策

**租戶與實例的對應關係在資料平面處理：抓取設定與 Prometheus 的 recording rule（預先計算並存成新序列的查詢）。threshold-exporter 不處理映射。** 三種拓撲各用一種做法：

1. **1:1（預設）**：一個 namespace 是一個租戶。抓取時把 `namespace` 寫成 `tenant`，不需要額外設定。
2. **N:1**：多個 namespace 屬於同一個租戶。抓取設定用 `relabel_configs` 的 regex 把這幾個 namespace 都標成同一個 `tenant`；`scaffold_tenant.py --namespaces ns1,ns2` 產生這段設定。
3. **1:N**：一個資料庫實例裡有好幾個邏輯租戶（例如 Oracle 的多個 schema）。在設定目錄根層放 `_instance_mapping.yaml`，由 `generate_tenant_mapping_rules.py` 產生 recording rule，把實例層級的序列按 filter 切開、各加上 `tenant`。

N:1 的一個常見情形是同一個租戶跑在好幾個叢集。這時每台 Prometheus 另外標出自己在哪個叢集，規則逐（租戶、叢集）計算，見下方「多叢集同租戶」。

### 範例：1:N 映射

設定目錄根層的 `_instance_mapping.yaml`（repo 內的範例檔）：

```yaml {.quote src="components/threshold-exporter/config/conf.d/examples/_instance_mapping.yaml"}
instance_tenant_mapping:
  # Oracle: single instance, multiple schemas → multiple tenants
  oracle-prod-01:
    - tenant: db-a
      filter: 'schema=~"app_a_.*"'
    - tenant: db-b
      filter: 'schema=~"app_b_.*"'

  # DB2: single instance, multiple tablespaces → multiple tenants
  db2-shared-01:
    - tenant: db-c
      filter: 'tablespace="ts_client_c"'
    - tenant: db-d
      filter: 'tablespace="ts_client_d"'
```

`generate_tenant_mapping_rules.py --config-dir <目錄> --metrics oracle_sessions` 的輸出（結束碼 0）：

```yaml
groups:
- name: tenant_mapping_db2-shared-01
  interval: 30s
  rules:
  - record: tenant_mapped:oracle_sessions:current
    expr: oracle_sessions{instance="db2-shared-01", tablespace="ts_client_c"}
    labels:
      tenant: db-c
  - record: tenant_mapped:oracle_sessions:current
    expr: oracle_sessions{instance="db2-shared-01", tablespace="ts_client_d"}
    labels:
      tenant: db-d
- name: tenant_mapping_oracle-prod-01
  interval: 30s
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

每個實例一個規則群組，每個（條目、指標）一條規則。`filter` 是逗號分隔的 `label=`、`label=~`、`label!=` 比對；其他寫法原樣放進選擇器。省略 `--metrics` 時，用 `metric-dictionary.yaml` 裡的每一個指標。`--format configmap` 把同樣的規則包成名為 `rules-tenant-mapping` 的 ConfigMap（key 是 `tenant-mapping-rules.yaml`）。

加上 `--validate` 時，檢查每個條目的租戶是否在設定目錄裡有租戶檔。上面的目錄若只有 `db-a`、`db-b` 的租戶檔，輸出下面三行並以結束碼 1 結束、不產生規則：

```text
ERROR: instance=db2-shared-01: tenant 'db-c' not found in config-dir
ERROR: instance=db2-shared-01: tenant 'db-d' not found in config-dir
ERROR: validation failed — fix errors above before generating rules
```

相關工具：`da-tools discover-mappings`（`discover_instance_mappings.py`）讀 exporter 的 `/metrics` 或查 Prometheus，列出實例裡 schema、tablespace 這類 label 的值，產生一份待填租戶名的 `_instance_mapping.yaml` 草稿；`scaffold_tenant.py --topology=1:N --mapping-instance <實例> --mapping-filter <filter>` 在它的草稿設定目錄裡多產生一份只含這個租戶的 `_instance_mapping.yaml`，要自己併進正式的那份。

### 多叢集同租戶：叢集身分與告警粒度

**同一個租戶可以出現在多個叢集。每台 Prometheus 用一個 external label `da_cluster` 說明自己在哪個叢集；凡是以租戶 exporter 序列為輸入的規則，聚合與比對都以（tenant、da_cluster）為單位，告警也逐叢集各發一次。**

這是 N:1 裡「多叢集同租戶、DR 對（主站與備援站各一套）」的契約。規則包、Alertmanager 的 warning／critical 去重、exporter 缺席告警、Kubernetes 的兩項「設定了卻沒有對應資料」檢查、租戶自助告警（`Custom_*`，`absence` recipe 除外）都已逐叢集；還沒逐叢集的檢查列在「後果與已知限制」，追蹤於 [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)。

#### 契約

1. **叢集身分的來源。** 每台 Prometheus 在 `global.external_labels` 設 `da_cluster: <叢集 ID>`，一台一行，抓取 job 不必寫任何東西。external label 是 Prometheus 在資料離開它時加上的：送往 Alertmanager 的告警、`/federate`、remote write 都會帶。所以兩個叢集的序列或告警相遇的地方（中央 Prometheus、Alertmanager）看到的資料已經分得出叢集；只評估自己叢集的 Prometheus，序列上沒有這個 label，規則的分組自然等同逐租戶。單一叢集的部署也要設，值固定即可。
2. **叢集 ID。** 在整個平台內唯一，每個叢集一個、不隨時間改變；同一個叢集裡的 Prometheus 副本（HA 對）用同一個值。已經照 [Federation 整合指南](../integration/federation-integration.md) 設了 `external_labels.cluster` 的部署，`da_cluster` 用同一個值、與它並列。
3. **閾值仍是租戶一份。** 閾值、維護狀態、租戶 metadata 由 threshold-exporter 送出，一個租戶一組，不分叢集。這些序列若是從別的叢集 federate 上來，身上也會帶 `da_cluster`；規則一律先以 `by(tenant)` 收成一份，再用 `on(tenant) group_left` 套到該租戶的每個叢集（`group_left` 表示右邊一條序列可以配左邊多條），所以帶不帶結果相同。前提是各叢集送上來的閾值內容一致。
4. **規則的寫法。** 輸入是租戶 exporter 序列的聚合，`by(...)` 只要含 `tenant`、`instance`、`pod`、`namespace`、`node` 任一個，就必須同時含 `da_cluster`。兩邊都是這類序列的配對（`on(...)`）也一樣。`check_cluster_identity.py`（pre-commit）讀規則的語法樹檢查這一點；刻意跨叢集看的規則，在規則正上方寫 `# da_cluster: tenant-wide <finding 種類> (<labels>) — <理由>` 宣告，例如 `TenantExporterReplicaDown`。
5. **告警帶 `da_cluster`。** 送到 Alertmanager 的告警都有這個 label：規則算出來的告警若已經帶著（它讀的序列來自哪個叢集），就是那個值；沒有的，Prometheus 送出時補上自己的 external label，也就是「評估這條規則的那台 Prometheus 所在的叢集」。Alertmanager 的 warning／critical 去重（[ADR-001](./001-severity-dedup-via-inhibit.md)）把它納入，兩個叢集的同名告警互不壓制。通知的分組（`group_by`，決定哪些告警合成同一則通知）預設不變：同一條告警在幾個叢集同時觸發時是一則通知，裡面裝著各叢集的告警；會不會逐條列出告警與它的 `da_cluster`，取決於接收端用的模板（見已知限制）。要每個叢集各收一則的部署或租戶，把 `da_cluster` 加進自己的 `group_by`。租戶自助告警（`Custom_*`）例外：它們走另一條分組固定為 `[tenant, alertname]` 的路由，租戶的 `group_by` 對它們不適用。只讀 threshold-exporter 序列的告警（例如「租戶沒設定閾值」）帶的是後一種值：它講的是整個租戶，label 上的叢集只代表是哪一台算出來的。
6. **「該有卻沒有」逐叢集判定。** 租戶可以在設定裡宣告自己預期出現在哪些叢集；有宣告時，任一個宣告的叢集沒有回報就告警。沒宣告時，只要在至少一個叢集出現就算存在。宣告的位置是租戶頂層的 `_expected_exporters`（見 [ADR-010](./010-multi-tenant-grouping.md)），它列出租戶跑哪些種類的 exporter：條目只寫種類（`mariadb`）就是沒宣告叢集；寫成 `種類@叢集`（`mariadb@edge-a`）就是宣告「這個叢集要有這一種 exporter 在回報」，其中叢集是該叢集 Prometheus 的 `da_cluster` 值。同一個種類只能擇一寫法，兩種並寫會被拒收。叢集 ID 只能用英數字與 `.`、`_`、`-`（英數字開頭與結尾，最長 63 個字元，大小寫有別）；`da_cluster` 的值含其他字元（例如 `/`、`:`）的叢集目前無法逐叢集宣告。逐叢集的缺席告警（`TenantExporterAbsent`）帶的 `da_cluster` 就是宣告的那個叢集。平台沒有叢集清單，不檢查宣告的叢集是否存在：叢集 ID 打錯時，結果是對那個不存在的叢集發缺席告警。
7. **沒設 `da_cluster` 的 Prometheus 在上線時被指出來。** `da-tools federation-check edge` 讀實際那台 Prometheus 的設定，`external_labels.da_cluster` 沒有值時 `edge_cluster_identity` 這項檢查失敗；`da-tools byo-check prometheus` 同樣讀實際載入的設定，沒有值時 `cluster_identity` 這項檢查失敗、結束碼 1（同一輪有呼叫錯誤時是 2）；對著回 404 的查詢層（Thanos Querier、VictoriaMetrics）時這項略過，要用 `federation-check edge` 逐台檢查。`da-tools onboard --scrape-config` 分析既有的 `prometheus.yml` 時，沒有值會在 stderr 印出 WARN，並寫進 `scrape-analysis.yaml` 的 `cluster_identity`（`set`、`missing`，檔案沒有 `global` 段時是 `unknown`）。本 repo 自己的 Prometheus 設定由 `check_scrape_reachability.py` 檢查同一件事（`CLUSTER-IDENTITY`）。這些檢查都只看一個地方，不需要推算 relabel 的結果。

這些條款由下列測試守住：`tests/rulepacks/rule-pack-cluster-masking_test.yaml`（一個叢集健康、另一個故障時只對故障的叢集告警）、`tests/rulepacks/test_cluster_metamorphic.py`（每個規則包測試加上叢集 label 重跑）與 `tests/dx/fixtures/custom_alerts_promtool/per_cluster.yaml`（每一種自助告警 recipe 在兩個叢集上的結果）守第 4 點；`tests/alertmanager-inhibit/inhibit_semantics_test.go` 守第 5 點的去重；`tests/rulepacks/rule-pack-liveness-per-cluster_test.yaml` 與 `components/threshold-exporter/app/pkg/config/expected_exporters_test.go` 守第 6 點；`tests/ops/test_federation_check.py`、`tests/ops/test_byo_check.py`、`tests/ops/test_onboard_platform.py` 與 `tests/lint/test_check_scrape_reachability.py` 守第 7 點。

#### 範例

本 repo 的 Prometheus 設定（每台各填自己的值）：

```yaml {.quote src="k8s/03-monitoring/configmap-prometheus.yaml"}
global:
# ...
  external_labels:
    da_cluster: lab
```

同一個租戶 `db-a` 在 `edge-a`、`edge-b` 各有一台位址同為 `10.0.0.5:50000` 的資料庫，`edge-a` 那台故障。中央 Prometheus 從兩個叢集收到的序列：

```text
db_expected{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}  1
db_expected{tenant="db-a", da_cluster="edge-b", instance="10.0.0.5:50000"}  1
db_up{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}        0
db_up{tenant="db-a", da_cluster="edge-b", instance="10.0.0.5:50000"}        1
```

配對不含叢集身分：沒有結果，不告警（`edge-b` 的成功把 `edge-a` 遮掉）。

```promql
max by(tenant, instance) (db_expected)
  unless on(tenant, instance)
max by(tenant, instance) (db_up == 1)
```

配對含叢集身分：一筆結果 `{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}`。

```promql
max by(tenant, da_cluster, instance) (db_expected)
  unless on(tenant, da_cluster, instance)
max by(tenant, da_cluster, instance) (db_up == 1)
```

`db_expected` 與 `db_up` 是為了說明而取的名字，不是平台的指標。規則包裡同樣形狀的實際案例（`DB2DatabaseDown`，兩個叢集的實例位址相同）在 `tests/rulepacks/rule-pack-cluster-masking_test.yaml`。

逐叢集宣告的實際樣子（第 6 點）。租戶 `db-a` 的設定：

```yaml
tenants:
  db-a:
    _expected_exporters: [mariadb@edge-a, mariadb@edge-b, redis]
```

`edge-a` 的 MariaDB exporter 消失、`edge-b` 的還在，Redis exporter 只在 `edge-b`。中央 Prometheus 上的序列：

```text
tenant_expected_exporter{tenant="db-a", pack="mariadb", expected_cluster="edge-a"}  1
tenant_expected_exporter{tenant="db-a", pack="mariadb", expected_cluster="edge-b"}  1
tenant_expected_exporter{tenant="db-a", pack="redis"}                               1
mysql_up{job="tenant-exporters", tenant="db-a", da_cluster="edge-b"}                1
redis_up{job="tenant-exporters", tenant="db-a", da_cluster="edge-b"}                1
```

三分鐘後有一則告警：`TenantExporterAbsent{tenant="db-a", pack="mariadb", da_cluster="edge-a"}`。`edge-b` 的 MariaDB 與只寫種類的 Redis 都不告警。同樣的序列，若租戶只寫 `_expected_exporters: [mariadb, redis]`，沒有任何告警：`edge-b` 有一台在回報就算存在。這兩種情形是 `tests/rulepacks/rule-pack-liveness-per-cluster_test.yaml` 的 L1 與 L2（測試用的叢集名是 `east`、`west`）。

### 基礎設施指標的租戶歸屬（Addendum）

**磁碟、PVC、節點這類基礎設施指標，只支援 1:1 的租戶歸屬（一個 namespace 是一個租戶）。**

[ADR-024](024-version-aware-threshold-via-dimensional-label.md) 的自助告警讓租戶對平台的基礎設施指標（例如 kubelet 的 `kubelet_volume_stats_*`）設條件。這些指標原生只帶 `namespace`、不帶 `tenant`，所以在抓取階段直接把 `namespace` 複製成 `tenant`。平台附的 Prometheus 設定（`k8s/03-monitoring/configmap-prometheus.yaml`）在 kubelet 抓取 job 的 `metric_relabel_configs` 這樣做，只保留 `db-` 開頭的 namespace。

## 背景

企業環境中，資料庫實例與租戶的對應並非總是一對一：

| 拓撲 | 說明 | 典型場景 |
|:-----|:-----|:---------|
| **1:1** | 一個 namespace／實例對應一個租戶 | 獨立部署的微服務、專用 DB |
| **N:1** | 多個 namespace／實例聚合為一個租戶 | 多叢集同租戶、DR 對、讀寫分離 |
| **1:N** | 單一實例內含多個邏輯租戶 | Oracle 多 schema、DB2 多 tablespace、共享 RDS |

**1:N。** 共享實例的指標只帶 `instance`，不帶租戶。exporter 不知道實例內部怎麼按 schema 或 tablespace 劃分給租戶，告警規則也就無法對同一個實例上的不同租戶套用不同的閾值。需要一個通用的方法把實例層級的指標拆成租戶層級，而不改 threshold-exporter。

**多叢集同租戶。** 規則裡有兩類寫法在同租戶跨叢集時會出錯：

- **互相遮蔽。** 存活判定寫成「預期存在的實例，扣掉有成功回報的實例」，兩邊以 `on(tenant, instance)` 配對。兩個叢集各有一台位址相同的實例時（各叢集內部網段重複很常見），健康的那台會把故障的那台消掉，故障的叢集不告警。
- **悄悄合併。** `sum by(tenant)` 會把兩個叢集加在一起，`max by(tenant)` 只留下較大的那個。值班人員看不出是哪個叢集，數值也不再對應任何一個資料庫。

**基礎設施指標。** 本 ADR 的 N:1 與 1:N 靠額外的映射規則，而基礎設施指標走的是抓取階段的直接改名，拿不到那層映射：

- **N:1 會靜默漏告警。** 指標被標成 `tenant="db-a-read"`，但租戶的設定用的是 `db-a` 這個名字，兩者對不上，告警永遠不觸發。
- **1:N 本質上做不到。** 一顆 PVC 無法按 schema／tablespace 拆給不同租戶。

## 後果與已知限制

### 1:N 映射

- **threshold-exporter 不感知拓撲。** 它不讀 `_instance_mapping.yaml`；三種拓撲最後都收斂成序列上的 `tenant` label。
- **原始序列與映射後的序列並存。** recording rule 產生新序列，原始的實例序列照留，兩種視角都查得到。代價是被映射的那部分序列數加倍：每條規則把它選到的原始序列各複製一份、多加 `tenant`。`--dry-run` 印出的估計是「條目數 × 指標數」，假設每條規則只選到一條序列。
- **映射後的序列晚至多一個評估間隔。** 產生的規則群組 `interval` 是 30s。
- **平台的規則包不讀映射後的序列。** 產生的序列名稱是 `tenant_mapped:<原指標>:current`，`rule-packs/` 裡沒有任何規則讀這個名字；要對它們告警，得自己寫規則。
- **產生的規則不在平台附的 Prometheus 部署裡。** `k8s/03-monitoring/deployment-prometheus.yaml` 的規則 projected volume 沒有 `rules-tenant-mapping` 這個來源，Prometheus 只載入 `/etc/prometheus/rules/*.yml`。要用就把這個 ConfigMap 加進那個 projected volume，並投影成 `.yml` 檔名；加進去之後，改動由 config-reloader sidecar 觸發 Prometheus 重新載入，不必重啟（[ADR-005](./005-projected-volume-for-rule-packs.md)）。
- **租戶存在與否的檢查要自己接進管線。** `--validate` 檢查映射裡的租戶在設定目錄裡有租戶檔，但 repo 的 CI 不跑這支工具。
- **沒有 schema 驗證。** `_instance_mapping.yaml` 沒有 JSON Schema；缺 `tenant` 或 `filter` 為空的條目以 WARN 略過，不讓工具失敗。
- **filter 要懂底層資料庫的結構。** 寫 filter 的人得知道 schema／tablespace 怎麼對應到 exporter 的 label。
- **沒有端到端驗證。** repo 內只有產生器的單元測試（`tests/ops/test_generate_tenant_mapping_rules.py`），沒有把產生的規則載進 Prometheus 的測試，也沒有在實際多 schema 的 Oracle 環境跑過。

### 多叢集同租戶的已知限制

- **升級順序不拘。** 規則在沒有 `da_cluster` 的序列上照樣運作，結果與不分叢集時相同；external label 設上去之後，離開那台 Prometheus 的資料才開始帶它。
- **告警在 Alertmanager 那一端多一個 label。** 設上 external label 之後，Alertmanager 把帶新 label 的告警視為另一條：新的立刻出現並通知一次，舊的那條要等到它過期才消失，中間兩條並存。
- **既有的 silence 仍然有效。** silence 以它列出的 label 比對，告警多一個 label 不影響。
- **在邊緣叢集本地看不到叢集。** 邊緣 Prometheus 的查詢、`ALERTS` 序列、告警 annotation 裡的 `$labels.da_cluster` 都是空的；值在資料到了 Alertmanager 或中央之後才有。
- **一台 Prometheus 直接抓好幾個叢集時不適用。** external label 只有一個值。這種部署要自己在各叢集的抓取 job 上寫 `da_cluster`（序列上已有的值優先於 external label），lint 不檢查那個寫法。
- **各叢集的閾值要一致。** 每個叢集各有一份 threshold-exporter 並 federate 到中央時，中央以 `by(tenant)` 把它們收成一份，取其中較大的值；內容不一致時，中央用的不是任何一個叢集自己的那組。
- **加總是逐叢集的。** 「該租戶所有實例加總」的規則（例如鎖等待、deadlock 次數）在每個叢集各自加總、各自比對閾值。
- **租戶自助告警（`Custom_*`）逐叢集判定。** 除了 `absence`，每一種 recipe 都以（租戶、叢集）為單位計算與比對：取最大值的（`threshold`）用該叢集自己的值；加總的（`rate`）只加該叢集的序列；算比值的（`ratio`、`forecast` 的比值模式、`slo_burn_rate`）分子與分母都只用該叢集的；分位數（`p99_latency`）與趨勢預測（`forecast`）也各叢集各算。`slo_burn_rate` 的長窗、短窗與壞事件下限要在同一個叢集同時成立，一個叢集的長窗不會與另一個叢集的短窗湊成一則告警。「等於某個代碼」的寫法（`op: "=="`）每個符合的叢集各發一則。告警帶 `da_cluster`，值是那個叢集自己的；閾值與維護狀態仍是租戶一份（第 3 點）。所以在收了多個叢集資料的 Prometheus 上，要兩個叢集相加才越過閾值的告警不觸發（兩個叢集各 40/s、閾值 50/s：不告警），被另一個叢集流量沖淡的比值照樣觸發（`east` 沒有錯誤、流量是 `west` 的一百倍，`west` 的錯誤率是 10%、閾值 5%：合算約 0.1%，逐叢集則 `west` 告警，值是 10%）。在序列沒有 `da_cluster` 的單一 Prometheus 上，結果與不分叢集時相同。這些情形由 `tests/dx/fixtures/custom_alerts_promtool/per_cluster.yaml` 守住。
- **`absence` recipe 維持「任一個叢集有就算有」。** recipe 沒有地方宣告指標該出現在哪些叢集，依第 6 點，沒宣告時在至少一個叢集出現就算存在。所以租戶的所有叢集都沒有這個指標時才告警；只有某個叢集缺、別的叢集還有時不告警。這則告警講的是整個租戶，規則算出來時不帶叢集（送出時補上的是評估它的那台 Prometheus 的叢集，同第 5 點後半）。編譯出來的規則檔在這條規則正上方寫 `# da_cluster: tenant-wide by-drops-cluster (tenant) — …`，註明它是刻意跨叢集判定的。
- **自訂告警不能只針對某一個叢集。** recipe 的 `selectors` 與 `selectors_re` 不接受 `da_cluster`，這一條自訂告警在載入時被拒，租戶的其他自訂告警照常載入。原因是序列只有在各叢集資料相遇的地方才帶這個 label：同一份設定在中央 Prometheus 上會只看指定的叢集，在各叢集自己評估規則的部署上卻沒有任何序列符合，告警不會響，也沒有提示。
- **逐叢集的存在判定要在序列帶 `da_cluster` 的地方評估。** 也就是收了各叢集資料的中央 Prometheus；規則分層部署時告警規則本來就放在那裡。各叢集拿同一份租戶設定、各自評估告警規則的部署，本地序列沒有 `da_cluster`，`種類@叢集` 在那裡等同只寫種類：本地有這一種 exporter 就全部算存在，本地沒有時則對該種類每一個宣告的叢集各發一則，告警上的叢集是本地 Prometheus 看不到的那些。這種部署請只寫種類，各叢集用各自的租戶設定。邊緣 Prometheus 若在抓取 job 上自己寫了 `da_cluster`、又拿共用的租戶設定自行評估，情況更糟：本地序列只帶本地那個叢集，其他宣告的叢集即使全部健康也會一直告警。
- **沒有 `da_cluster` 的存在序列，對每個宣告的叢集都算存在。** 上一項的另一面：中央 Prometheus 若自己也直接抓租戶 exporter，那份本地序列會讓所有宣告的叢集都不告警。要逐叢集判定，就在那個抓取 job 上寫 `da_cluster`。
- **「設定了卻沒有對應資料」的兩項 Kubernetes 檢查逐叢集判定，不需要宣告。** 「設了自訂 recipe 的磁碟告警卻沒有對應資料」（`CustomRecipeDiskInert`）以（租戶、叢集）為單位：租戶在某個叢集有 Pod 在跑、那個叢集卻沒有它的磁碟資料，就對那個叢集告警，別的叢集有資料不算數；租戶沒有 Pod 在跑的叢集不告警。「設了依版本區分的閾值卻沒有版本資料」（`VersionAwareThresholdInert`）以叢集為單位：設了這種閾值的租戶有 Pod、卻完全沒有 Pod label 資料的叢集各發一則。它只看容器 CPU 的版本閾值；只設了 memory 版本閾值的租戶不在這條告警的範圍內（它的閾值同樣會因為沒有 Pod label 而不生效，但這條告警不會指出來）。只跑其他租戶的叢集不告警，那裡沒有要套用的版本閾值。兩條告警都帶 `da_cluster`，annotation 會寫出是哪個叢集。在序列都沒有 `da_cluster` 的單一 Prometheus 上，設了版本閾值的租戶全都沒有 Pod 時，`VersionAwareThresholdInert` 不響，即使別的租戶有 Pod、也沒有 Pod label 資料；租戶的第一個 Pod 出現後才會響。帶 `da_cluster` 的情形由 `tests/rulepacks/rule-pack-kubernetes-sentinels-per-cluster_test.yaml` 守住，單一 Prometheus 的情形由 `tests/rulepacks/rule-pack-kubernetes-sentinel_test.yaml` 的案例 F、F2 守住。
- **這兩項檢查假設同一個叢集的兩種資料帶同一個 `da_cluster`。** 兩種資料指的是「有 Pod 在跑」與它要對照的那一種（磁碟資料、Pod label 資料）；它們經過同一台 Prometheus，所以要嘛都帶、要嘛都不帶。假設不成立時，結果可能是誤報，也可能是漏報。誤報：一種帶 `da_cluster`、另一種不帶，或同一個叢集同時經由帶 label 的 federation 與不帶 label 的本地抓取進來、而對照的資料只在其中一條路徑上，告警會對其實正常的叢集響（不分叢集時不響）。漏報：對照的資料以「另一個叢集」的身分進來時會替它消掉告警。例如叢集 A 的 Pod 資料不帶 label、也真的沒有 Pod label 資料，而叢集 B 的 Pod label 資料不帶 label，B 的資料就被當成 A 的。這裡刻意不採用「沒有 `da_cluster` 的資料替每個叢集都算數」的寬鬆規則（exporter 缺席告警有這條）：那樣中央 Prometheus 自己抓的叢集有資料時，會遮住另一個真的缺資料的叢集。中央自己抓的叢集（不帶 label）與 federate 上來的叢集（帶 label）並存時，兩邊各自判定，結果正確。
- **還沒逐叢集的檢查。** 「多個租戶的 exporter 同時缺席」的彙總告警（`MassExporterOutage`）以（租戶、種類）為單位計數，一個種類宣告了幾個叢集都算一組，規則算出的告警不帶 `da_cluster`（到 Alertmanager 時只帶評估它那台 Prometheus 的叢集），不會指出是哪個叢集大量缺席。租戶自助告警的 `absence` recipe 不分叢集（見上）。「同租戶還有健康的抓取目標、但其中一個掛了」的告警（`TenantExporterReplicaDown`）刻意維持跨叢集比對：只寫種類的租戶某個叢集整個掛掉時，只要那個叢集的抓取目標仍被發現、讀到 `up=0`，那是唯一的訊號。目標本身已經不在（例如 Service 被移除）時這條告警也不會響，而只寫種類的租戶因為另一個叢集有回應，exporter 缺席告警（`TenantExporterAbsent`）同樣不響：這種情形沒有任何訊號，要逐叢集宣告才有。進度見 [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)。
- **維護模式是整個租戶。** 無法只對一個叢集進維護。
- **各叢集不能有不同的閾值。** 同一個租戶在所有叢集用同一組閾值。
- **通知預設不按叢集拆。** 平台不附通知模板，看到的是 Alertmanager 內建模板的輸出。兩個叢集同時觸發時，email 內文、PagerDuty 的 details、webhook payload 逐條列出告警與它的 `da_cluster`；單行標題是 `[FIRING:2] <告警名> <租戶> (<其他共同 label>)`，沒有叢集（只有一個叢集觸發時，標題會帶出叢集，因為那時它是整組共同的 label）。內建的 Slack 模板只有標題、沒有內文，兩個叢集同時觸發時看不出是哪兩個，要用逐條列出告警的自訂模板。PagerDuty 一組是一個 incident，所有叢集都恢復後才 resolve。預設不改的原因：租戶的 route 帶不帶 `group_by`，看部署的 `_routing_defaults`、routing profile 與租戶自己的 `_routing` 合起來最後有沒有值（`da-tools init` 產生的 `_defaults.yaml` 有給）；最後沒有值的租戶（哪一層都沒給，或租戶把它寫成 `null` 或空列表蓋掉）才沿用根 route 的分組，所以改平台預設值只會影響到這一部分租戶，不是全部；另一個原因是下面的第一項代價。
- **把 `da_cluster` 加進 `group_by` 有兩項代價。** 一、講整個租戶的告警（第 5 點後半）帶的是評估它的那台 Prometheus 的叢集，幾台 Prometheus 評估同一批規則時會變成每台各一則。二、在運作中的 Alertmanager 上改一條 route 的 `group_by`，該 route 當下觸發中的每一組都會再通知一次（分組的 key 變了），以分組為 key 的接收端（PagerDuty）會開新的 incident；舊的 incident Alertmanager 不會替它 resolve（恢復通知送到的是新的 key），要手動關閉。設定寫法見 [BYO Alertmanager 整合指南](../integration/byo-alertmanager-integration.md)。

### 基礎設施指標

- **N:1 客戶在遷移當天就會收到訊號。** [ADR-024](024-version-aware-threshold-via-dimensional-label.md) 的看門告警 `CustomRecipeDiskInert`：租戶設了磁碟告警、它的 Pod 也在跑，但歸屬後的磁碟指標一直沒出現，它就會響。N:1 客戶因此收到「這條告警沒生效」的明確訊號，而不是無聲漏報。
- **N:1 與 1:N 不支援。** 等真有 N:1 客戶（例如讀寫分離的資料庫）需要基礎設施告警，再把映射補上。

## 考慮過的替代方案

### 1:N 映射放在哪一層

- **在 exporter 內建映射**：單一元件處理，但 exporter 的職責是把 YAML 設定轉成指標，映射屬於資料正規化，放進去就越界；映射一改就要重啟 exporter，影響所有租戶的指標；HA 部署下兩個 exporter 還得維持一致的映射。
- **在 Alertmanager 映射**：只影響通知路徑，但太晚：Alertmanager 只處理已觸發的告警，無法在指標層級拆分；Grafana 這類工具直接查 Prometheus，看不到映射。
- **外部 proxy**：完全解耦，但多一個元件、多一段延遲、運維更複雜。

### 多叢集的叢集身分

- **沿用 `cluster` 這個名字。** RabbitMQ、Elasticsearch 的 exporter 自己就輸出 `cluster`（該服務自己的叢集名），而 Prometheus 遇到同名 label 有兩種行為，兩種都會出錯。預設情況下保留抓取設定寫的值、把 exporter 的改名為 `exported_cluster`，讀 exporter 那個 `cluster` 的規則就讀到平台的叢集 ID。設了 `honor_labels: true` 時（中央從邊緣拉資料的 federation job 就是）保留 exporter 的值，叢集身分被換成服務自己的叢集名。所以用平台專屬的名字。
- **在每個抓取 job 用 relabel 寫入 `da_cluster`。** 好處是規則評估時序列上就有值。但要寫的地方有四處（租戶 exporter、容器指標、volume 指標、Pod 狀態指標），其中一處還得限定在單一 Service 上；只寫了一部分時，需要把兩種來源配對的容器告警會配不上而靜默。要在部署前擋下這種設定，檢查工具就得靜態推算 relabel 的結果，等於重做一遍 Prometheus 的行為，補不完。而規則真正需要叢集身分的地方只有兩個叢集的資料相遇之處，那裡的資料已經帶著 external label。
- **只在已經帶 `instance` 這類 label 的地方加 `da_cluster`，租戶層級的聚合維持跨叢集一份。** 要改的地方少得多。但規則分層部署時（[ADR-004](./004-federation-central-exporter-first.md)、[Federation 整合指南](../integration/federation-integration.md) §8 的邊緣評估），租戶層級的 recording rule 是在各邊緣叢集算的：兩個邊緣各送一條同名序列到中央，結果與「全部在中央評估」時不同。逐叢集是兩種部署方式下結果一致的最粗粒度。比值類的規則也無法把各邊緣的比值再合成整個租戶的比值。
- **不支援，並擋下跨叢集的租戶。** 要擋也得先有叢集身分；而 DR 對與多地部署正是 N:1 列出的典型場景。

## 相關

- [ADR-001](./001-severity-dedup-via-inhibit.md) — 嚴重度去重的 inhibit 規則，逐叢集配對
- [ADR-004](./004-federation-central-exporter-first.md) — Federation 架構
- [ADR-005](./005-projected-volume-for-rule-packs.md) — 規則包以 projected volume 分發
- [ADR-010](./010-multi-tenant-grouping.md) — `_expected_exporters`
- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) — 自助告警與基礎設施指標
- [Federation 整合指南](../integration/federation-integration.md)
- [BYO Alertmanager 整合指南](../integration/byo-alertmanager-integration.md)
- [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097) — 尚未逐叢集的檢查
- [Prometheus Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/)
