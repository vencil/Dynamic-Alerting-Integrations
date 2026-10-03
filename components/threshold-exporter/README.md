# Threshold Exporter (v2.9.0)

<!-- 標題版號 = 最後 released tag。Release wrap 切六線 tag 時，本標題 + §Deploy 的 helm --version 跟著批次同步 bump。 -->

> 把告警閾值寫成 YAML、SHA-256 熱重載的多租戶 Prometheus exporter——**改一個數字即生效，不重啟、不碰 Prometheus rule**。

**先跑起來？** → **[QUICKSTART.md](QUICKSTART.md)**（build + 跑，≤5 分鐘看到 YAML 閾值變 live metric）。本篇 README 是配置與營運的**參考手冊**。

> **這份文件給誰？** 給操作 exporter、撰寫平台／領域 `_defaults.yaml` 與 recipe 的 **Platform Engineer** 與 **Domain Expert**。
> 你是 **Tenant**（只想調自己租戶的閾值或加告警）→ 直接走 **[Tenant 入門](../../docs/getting-started/for-tenants.md)** 用 portal 自助，通常不必碰這份參考。
> 不確定角色？→ **[選擇你的角色](../../docs/getting-started/README.md)**。

**相關文件：** [Helm chart](../../helm/threshold-exporter/) · [架構與設計](../../docs/architecture-and-design.md) · [遷移指南](../../docs/migration-guide.md) · [Rule Packs](../../rule-packs/README.md) · [版本歷程 CHANGELOG](../../CHANGELOG.md)

---

## 目錄

1. [這是什麼](#1-這是什麼) — 一分鐘理解輸入 / 輸出 / 邊界
2. [核心能力](#2-核心能力) — 六項能力速覽
3. [營運參考](#3-營運參考) — endpoints / 旗標 / metrics / exit codes
4. [配置參考](#4-配置參考) — 檔案規則 / 繼承 / 閾值語法 / 自訂告警
5. [配套 CLI](#5-配套-cli) — da-guard / da-parser / da-batchpr
6. [部署](#6-部署) — Helm + 熱重載模型
7. [開發](#7-開發) — build / test / 驗證

---

## 1. 這是什麼

把 `conf.d/` 裡的 YAML 配置轉成 Prometheus 閾值 metrics 的 **config-driven exporter**。平台團隊只維護一套告警 rule，各租戶的閾值由 YAML 決定——改 YAML、ConfigMap propagate、exporter 熱重載，**全程不重啟、不掉 scrape**。

| | |
|---|---|
| **輸入** | `conf.d/` 下的 `*.yaml` / `*.yml`（副檔名不分大小寫；`.` 開頭的檔與目錄略過）——單檔扁平 _或_ `<domain>/<region>/<tenant>.yaml` 階層；由 K8s ConfigMap volume 在 runtime 注入 |
| **輸出** | Prometheus gauge `user_threshold{tenant, component, metric, severity, …}` + 數個營運狀態 gauge + 一組 reload 觀測 metrics |
| **解決的問題** | 千租戶場景下，避免「改一個閾值要動 PromQL recording rule + 重啟 Prometheus」 |
| **不做的事** | 不執行 PromQL（只輸出 threshold gauge）· 不做告警路由（交給 Alertmanager）· 不持久化（無狀態，ConfigMap 是唯一真實來源） |

> 9 個核心設計概念（Severity Dedup / Sentinel Alert / 四層路由 / Dual-Perspective / Tenant API …）見 [架構與設計 §設計概念總覽](../../docs/architecture-and-design.md#設計概念總覽)。本 README 只負責營運層的 quick-reference。

---

## 2. 核心能力

| 能力 | 一句話 |
|------|--------|
| **Config-driven 閾值** | YAML 數字直接變 `user_threshold` metric；三態（自訂值 / 套預設 / 停用；適用條件見 [§4.3](#43-三態--嚴重度)）+ 嚴重度後綴 |
| **四層繼承** | 平台 → domain → region → tenant 逐層 deep-merge，租戶只寫差異 |
| **熱重載** | SHA-256 內容比對 + debounce；ConfigMap 變更後幾十秒內自動套用，不重啟 Pod |
| **維度標籤** | 同一 metric 依 label（精確或 regex）設不同閾值，無爆增 series |
| **自訂告警** | 租戶用平台 recipe 宣告自己的告警，**完全不寫 PromQL**（見 [§4.5](#45-自訂告警-_custom_alerts)） |
| **Cardinality Guard** | 每租戶 metric 數上限 + 確定性截斷，並用 gauge 把超限量曝露出來 |

> **版本歷程** 一律見 [CHANGELOG](../../CHANGELOG.md)（本 README 不重複維護「What's New」清單，避免過時）；升級風險點見 [遷移指南](../../docs/migration-guide.md)。

---

## 3. 營運參考

### 3.1 Endpoints

| Path | Method | 用途 |
|------|--------|------|
| `/metrics` | GET | Prometheus scrape（Go runtime metrics + 本 exporter 自訂 collector + promhttp handler 自身的錯誤計數）。gather 失敗（例如兩個 key 產生同一條 series）時整次回 500、`up` 歸 0（`ThresholdExporterDown` 會 fire）；成因看 exporter log 裡 `error gathering metrics:` 那一行（#2032） |
| `/health` | GET | Liveness probe（process 起來即 200） |
| `/ready` | GET | Readiness probe（config 載入完成才回 200，否則 503） |
| `/api/v1/config` | GET | Resolved config + 租戶清單（debug；支援 `?at=<RFC3339>` 模擬未來時間點） |
| `/api/v1/config/identity` | GET | 目前服務的那一版設定的 `config_hash` 與這一版位元組無法 parse 的檔案 `parse_failed`——**給機器讀的契約**（`schema: 1`；patch-config 寫後驗收用），欄位見 [API Reference §5](../../docs/api/README.md) |
| `/api/v1/tenants/simulate` | POST | Ephemeral 合併預覽——帶 base64 的 tenant YAML + defaults chain，回傳 `merged_hash` + 完整 inheritance 預覽。**不寫 disk、不改 manager 狀態**。exporter 載入時會整份丟棄的 payload（租戶檔、或 chain 根層 L0 `_defaults.yaml` 過不了 exporter 自己的完整解析，例如 `defaults` 值是字串）回 **400**，`{error}` 點名是 `tenant_yaml` 還是 `defaults_chain_yaml[0]`（[#1981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1981)），錯誤最多列 10 條再附總數；租戶檔或 L0 會被整份丟棄時，即使 `tenant_id` 不在檔內也優先回 400 而非 404。L1 以下的 chain 檔值的型別寬鬆，與 exporter 相同；但 L1 以下出現 YAML 語法錯誤時 simulate 仍回 400（exporter 只丟該層），追蹤於 [#2296](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2296) |

### 3.2 旗標 / 環境變數

| 旗標 | 環境變數 | 預設 | 說明 |
|------|---------|------|------|
| `-config-dir` | `CONFIG_DIR` | (auto) | conf.d 目錄路徑（**優先**，推薦） |
| `-config` | `CONFIG_PATH` | (auto) | 單檔 legacy 模式 |
| `-listen` | `LISTEN_ADDR` | `:8080` | HTTP listen address |
| `-reload-interval` | — | `30s` | 熱重載 watch tick 間隔 |
| `-scan-debounce` | — | `300ms` | 變更 burst 的合併窗口（設 `0` 停用、回同步行為） |
| `-free-os-mem-after-reload` | — | `false` | 每次 reload 後主動把閒置 heap 還給 OS（持續高頻 reload 才需要；代價是每次多一次 GC） |

> **自動偵測序**：`CONFIG_DIR` → `-config-dir` → `CONFIG_PATH` → `-config` → `/etc/threshold-exporter/conf.d/`（目錄存在時）→ `/etc/threshold-exporter/config.yaml`。

### 3.3 Metrics

**閾值域**（Prometheus 告警 rule 直接消費；label set 隨配置動態變化）：

| Metric | Type | 用途 |
|--------|------|------|
| `user_threshold` | Gauge | resolved 閾值（labels: tenant / component / metric / severity + 維度 labels；自訂告警為 `component="custom"`） |
| `user_state_filter` | Gauge | 狀態型告警 filter 旗標（label: tenant / filter / severity） |
| `user_silent_mode` | Gauge | silent mode 生效中（告警仍進 TSDB，只抑制通知；label: tenant / target_severity） |
| `user_severity_dedup` | Gauge | critical 觸發時抑制 warning 通知（label: tenant / mode） |
| `user_slo_objective` | Gauge | 租戶在 `slo_burn_rate` 自訂告警宣告的 SLO 目標百分比；`objective: "disable"` 不發（labels: tenant / recipe_id；ADR-031） |
| `tenant_metadata_info` | Gauge | 租戶 `_metadata` 的資訊指標，值恆 1；Rule Pack 以 `group_left(runbook_url, owner, tier)` join 進告警（labels: tenant / runbook_url / owner / tier） |
| `tenant_expected_exporter` | Gauge | per-tenant exporter liveness 期望，值恆 1（labels: tenant / db_type；**僅對宣告 `_metadata.db_type` 的租戶 emit**）。`TenantExporterAbsent` anti-join 的左手邊（#869） |
| `da_config_event` | Gauge | timed config 失效事件（silent / maintenance 自動解除；labels: tenant / event / reason / target_severity） |
| `da_custom_alert_parse_errors` | Gauge | 每租戶被丟棄的 `_custom_alerts` 數（fail-loud；0 = 全數有效；label: tenant） |
| `da_tenant_metrics_over_limit` | Gauge | 每租戶超出 cardinality 上限的量（`max(0, 產出數 − 上限)`；持續超限就持續報該值；label: tenant） |
| `da_config_deprecated_keys` | Gauge | 每租戶設定裡仍在用的舊 key 拼法數（#1231 改名過渡期；只供觀察遷移進度，沒有告警；label: tenant） |

**營運域**（觀測 exporter 自身的熱重載健康）：

| Metric | Type | 用途 |
|--------|------|------|
| `threshold_exporter_config_info{config_source,git_commit}` | Gauge | 設定來源與 git revision，值恆 1（GitOps drift 觀察用） |
| `da_config_reload_trigger_total{reason}` | Counter | reload 次數，reason: `source` / `defaults` / `new` / `delete` / `forced` |
| `da_config_scan_failures_total{reason}` | Counter | watch 路徑掃描失敗次數（[#2452](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2452)），reason 為封閉集合：`duplicate_tenant`（同一租戶 id 在兩個檔宣告；有無 `_defaults.yaml` 兩種目錄模式都會）/ `walk_error`（設定目錄不存在或不是目錄）/ `root_unreadable`（設定目錄存在但列不出內容，例如權限不足；[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)）/ `empty_tree`（掃描結果沒有任何可用的設定檔：全被刪除或全讀不到；#2592），全部從 0 起。失敗時不套用任何變更、上一列的 reload 計數不動（#2592 前，後兩種情況在階層模式會把每個租戶都計成 `delete`）、`/ready` 仍 200；持續失敗每 tick 加 1，失敗出現前已排定、尚未執行的 debounced reload 再各加 1。告警 `ConfigScanFailing`：有掃描失敗，且 `da_config_last_scan_complete_unixtime_seconds` 已超過 5 分鐘沒更新（設定已超過 5 分鐘無法掃描） |
| `da_config_unreadable_files{reason}` | Gauge | 最近一次掃描因為讀不到而略過的設定檔／子目錄數（[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)），reason 為封閉集合：`stat_error`（stat 失敗，例如懸空的 symlink）/ `read_error`（讀檔失敗，例如權限不足）/ `walk_error`（根目錄以下的子目錄列不出內容，一個目錄計 1），全部從 0 起。被略過的檔裡的租戶不會出現在 `/metrics`，其他租戶照常 reload；每次完成的掃描都重設，修好後回 0。掃描本身失敗（設定目錄不見或不是目錄）時保留上一輪的值，那時 `ConfigScanFailing` 會響（`reason="walk_error"`）。根目錄本身列不出內容不在此計，而是掃描失敗（上一列的 `root_unreadable`）。告警 `ConfigFilesUnreadable`（critical：讀不到的檔讓其中的租戶整個從 `/metrics` 消失、所有閾值告警停止，子目錄讀不到時底下全部租戶一起消失）：`> 0` 持續 10 分鐘 |
| `da_config_defaults_unusable{reason}` | Gauge | 目前這份設定無法使用的 `_defaults.yaml` 檔數（兩種副檔名、任何層級；[#2592](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2592)），reason：`parse_failure`（解析失敗，整組 defaults 被丟掉，ADR-017）/ `unreadable`（stat 或讀檔失敗），兩者從 0 起。根目錄那份無法使用時，所有租戶從它繼承的閾值都消失（對應的 `user_threshold` series 不見）。整個子目錄列不出內容（`walk_error`）時，裡面的 `_defaults.yaml` 不計入這裡（掃描看不到它），該目錄下的租戶整批消失由 `da_config_unreadable_files{reason="walk_error"}` 與 `ConfigFilesUnreadable`（critical）兜底。與只在 reload 讀到壞檔那一刻才增加的 `da_config_parse_failure_total` 不同，這個值在檔案修好前一直維持，修好後回 0：`parse_failure` 在每次目錄模式的 config commit 重設；`unreadable` 在每次完成的掃描重設（讀不到的檔不算變更偵測的輸入，刪掉或新增它都不會觸發 commit），掃描本身失敗時保留上一輪的值。告警 `ConfigDefaultsUnusable`（critical）：`> 0` 持續 10 分鐘，檔案壞多久就響多久 |
| `da_config_reload_duration_seconds` | Histogram | 完整 reload 耗時（scan + parse + merge + commit） |
| `da_config_scan_duration_seconds` | Histogram | 目錄掃描耗時 |
| `da_config_initial_load_duration_seconds` | Gauge | 啟動時那一次載入的秒數；只在載入成功時設一次，之後的 reload 不動它。HTTP server 在載入完成後才啟動，startupProbe 的上限要大於這個值（[#2153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2153)） |
| `da_config_max_tenants_per_file` | Gauge | 所有檔案中，單一檔案 `tenants:` 宣告的租戶數最大值；整棵樹一個值、無檔名 label；解析失敗的檔不計；每次 config commit 重設（#2153） |
| `da_config_max_mapping_keys` | Gauge | 所有檔案中，單一 mapping（`defaults`／`state_filters`／`tenants`／單一租戶覆寫／`profiles`／單一 profile）鍵數最大值。YAML 解碼對 mapping 的鍵兩兩比對，成本隨鍵數平方成長。只計 exporter 解碼進設定的 mapping（未知 key、巢狀 `_` 檔、被檔案擺放規則剝掉的區塊、解析失敗的檔不計）；每次 config commit 重設（#2153） |
| `da_config_debounce_batch_size` | Histogram | 每次 fire 吸收的 trigger 數（debounce 健康指標） |
| `da_config_parse_failure_total{file_basename}` | Counter | YAML parse 失敗次數（定位壞檔）。租戶檔：**每次掃描計一次**（#1957 起由 walker 計、平面層不重計）；注意 watch 偵測到變更的那個 tick 會掃兩次（detectChange＋reload），所以「每次掃描」≠「每次 reload」。壞掉的 defaults 檔：平面層計一次，**另外每個受影響租戶再計一次**（刻意的，計數即影響範圍；例如根目錄 `_defaults.yaml` 壞、底下 3 個租戶，一次冷載計 4） |
| `da_config_defaults_change_noop_total` | Counter | 純 cosmetic 的 `_defaults` 變更（註解 / 排序，無實質影響） |
| `da_config_defaults_shadowed_total` | Counter | `_defaults` 變更被租戶 override 擋下的數量 |
| `da_config_blast_radius_tenants_affected{reason,scope,effect}` | Histogram | 每次 tick 受影響租戶的分佈 |
| `da_config_last_scan_complete_unixtime_seconds` | Gauge | 上次掃描完成時間（`time() − 此值` = 卡住偵測） |
| `da_config_last_reload_complete_unixtime_seconds` | Gauge | 上次 reload 完成時間 |
| `da_config_free_os_memory_total` | Counter | 主動還記憶體給 OS 的次數（未開 `-free-os-mem-after-reload` 時恆 0） |
| `da_config_subtree_undeliverable_tenants` | Gauge | 繼承了「只存在於子目錄 `_defaults.yaml`」之 key 的租戶數。`/effective` 會列出該 key 的值，但 collector 不會為它產生 `user_threshold`——collector 只走 conf.d **根目錄** `_defaults.yaml` 與宣告面（`optional_overrides:`），巢狀 `_defaults.yaml` 兩者都不餵——所以該租戶在這個 key 上的告警永遠不會觸發；租戶的其他 key 照常送出。暫行解法：把 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`；`_` 開頭的 key 只能宣告在根目錄 `_defaults.yaml`（`optional_overrides:` 不服務它們，宣告在那裡只會讓該 key 不再計入本 gauge、值仍不送出）；根本解（交付子目錄範圍，或在驗證時拒收）追蹤於 [#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)。同一份判定中的閾值 key 也在 merge 前由 `da-guard` 以 `subtree_default_undeliverable` warning 報出（下一個 minor 改為 error），`da-guard served-values` 把它列進 `unserved`；保留鍵與 exporter 本來就不產生閾值列的 `_silent_*`、`_state_*` 等鍵仍計入本 gauge，da-guard 不以這個 finding 報，改由 `subtree_default_reserved_key` 報（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)；`_routing`／`_routing_*` 由 routing 檢查報），被關掉的 key 也不報。每次 config commit 重設；ERROR log 點名租戶、來源檔與 key，**只在受影響集合「變化」時印一次**（含冷啟動、以及歸零後再度發生）。⚠️ **沒有出貨任何 PrometheusRule**，`> 0 for 10m` 是合理起點。⚠️ **BREAKING（#1957）**：取代舊的 conf.d 掃描器分歧 gauge（舊名見 CHANGELOG）；舊 gauge 的另一成因（同一個檔被兩平面解析出不同租戶）已因共用同一份完整解析而消失，故移除；兩平面租戶集合仍有一個已知例外——增量 tenant-only reload 對壞檔保留最後正確值，而 tenant-api 回 404、da-guard 以 exit 3 把該檔列入 `parse_failed`（[#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980)）；根目錄 `_` 開頭檔案裡 `tenants:` 給既有租戶的值兩平面都套用（[#1982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1982)、[#2019](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2019)） |
| `promhttp_metric_handler_errors_total{cause}` | Counter | `/metrics` handler 失敗次數，cause: `gathering` / `encoding`（兩條皆從 0 起）。gather 失敗時整次 scrape 回 500，所以這個值**只在恢復後第一次成功 scrape 才看得到**——供事後回溯，不拿來告警；即時訊號是 `up == 0`（#2032） |

### 3.4 Exit Codes（CLI binaries）

| Code | `da-guard` | `da-parser` | `da-batchpr` |
|------|-----------|-------------|--------------|
| 0 | clean | parse OK | 全部目標成功 |
| 1 | 發現錯誤（擋 merge） | gate 失敗（non-portable / ambiguous） | 一個以上目標失敗 |
| 2 | caller error（旗標 / 路徑） | caller error | caller error |
| 3 | exporter 無法 decode 的設定檔（見 [cli-reference §guard](../../docs/cli-reference.md#guard)；#2123） | — | — |

---

## 4. 配置參考

### 4.1 檔案邊界規則

**誰寫哪個檔**：`_*.yaml`（平台 / 各層 `_defaults.yaml`、policy、recipe 定義）由 **Platform Engineer / Domain Expert** 維護；`<tenant>.yaml`（含 `_custom_alerts`）是 **Tenant 自己的**，且通常經由 portal 代寫而非手改 YAML。下表是各檔允許的區塊：

| 檔名 pattern | 允許區塊 | 違規行為 |
|-------------|---------|---------|
| `_*.yaml`（如 `_defaults.yaml`） | `defaults` / `state_filters` / `tenants`（通常只放 defaults） | — |
| `<tenant>.yaml` | 僅 `tenants`（含子鍵 `_metadata` / `_silent_mode` / `_state_maintenance` / `_severity_dedup` / `_custom_alerts`） | 其他區塊自動忽略 + WARN log |

> 同一租戶 id 同時出現在扁平與階層路徑 → `Load()` 直接拒絕（保留前一份 known-good config），不靜默 last-wins。

### 4.2 四層繼承

```
conf.d/
├── _defaults.yaml             L0 平台預設
└── mysql/
    ├── _defaults.yaml         L1 domain 預設
    └── us-east/
        ├── _defaults.yaml     L2 region 預設
        └── db-a.yaml          L3 tenant override
```

合併語義：**deep merge**（map 遞迴）+ **array 整包替換**（不串接）+ **null 即刪除**（下層設 `null` 等同顯式否決）+ **前綴保留**（`_state_*` / `_routing*` / `_metadata` 等只允許在 `_` 前綴檔）。

⚠️ **子目錄 `_defaults.yaml` 的 defaults 不支援保留鍵**（`_state_*`、`_silent_mode`、`_severity_dedup`、`_metadata` 等）：目前 exporter 對它們只套用 `disable` 或數值、其他值丟掉（`_state_*` 在子目錄關得掉、開不了），`da-guard` 以 `subtree_default_reserved_key` warning 報出（值為 null 視為沒寫、不報；`_custom_alerts` 寫在檔案頂層是 custom-alert 編譯器的合法寫法、不報）。**下一個 minor 版起 exporter 不再套用子目錄 defaults 中的這些鍵，該 finding 改為 error**——請改寫在租戶自己的 `tenants:` 條目，`_state_<filter>` 也可改根目錄 `state_filters.<filter>.default_state`（影響樹中所有租戶；只想影響子目錄時請寫在各租戶條目）；根目錄未宣告的 filter 與非受承認的 `_silent_*` 等鍵寫在租戶條目也沒人讀，請宣告或刪除，訊息會依鍵給出修法（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)）。

### 4.3 三態 + 嚴重度

| 設定 | Prometheus 輸出 |
|------|----------------|
| `"70"` | `user_threshold{…} 70`（severity=warning） |
| `"40:critical"` | `user_threshold{…,severity="critical"} 40` |
| 省略不寫 | 套繼承來的 default（⚠️ 僅限 `_defaults.yaml` 的 `defaults:` 有值的 key） |
| `"disable"` | 不產生 metric |

⚠️ **宣告 key 只有兩態**：列在 `_defaults.yaml` 頂層 `optional_overrides:` 的 key，平台只認得 key 名、不主張值（`resolveDeclaredRows`）——沒有 default 可繼承，**省略＝沒有值＝不產生 metric**，不是「套預設」。

### 4.4 閾值語法速查

| 形式 | 範例 key | 範例 value | 備註 |
|------|---------|-----------|------|
| 純量閾值 | `mysql_connections` | `"70"` | 最常見 |
| 維度標籤（精確） | `"redis_queue_length{queue='tasks'}"` | `"500:critical"` | YAML key 必加引號；不繼承 defaults |
| 維度標籤（regex） | `"oracle_tablespace{tablespace=~'SYS.*'}"` | `"95"` | 輸出 `tablespace_re` label，交 PromQL `label_replace` 匹配 |
| 排程式閾值 | `mysql_connections:` | `default + overrides[]` | UTC `HH:MM-HH:MM` 窗口，多窗口 first-match-wins |
| 租戶 metadata | `_metadata` | `{runbook_url, owner, tier, …}` | 注入 `tenant_metadata_info` |
| Silent mode | `_silent_mode` | `"warning"` 或 `{target, expires, reason}` | `expires` 自動失效 |
| Maintenance | `_state_maintenance` | `{target, expires, reason, recurring[]}` | 窗口內抑制狀態告警 |
| Severity dedup | `_severity_dedup` | `"enable"` | `"enable"`（預設）/ `"disable"`；critical 觸發時抑制 warning 通知 |
| 自訂告警 | `_custom_alerts` | recipe 清單 | 見 [§4.5](#45-自訂告警-_custom_alerts) |

範例配置：

- 單一 DB — [`config/conf.d/db-a.yaml`](config/conf.d/db-a.yaml)
- 多 DB 維度 / 路由 — [`config/conf.d/examples/`](config/conf.d/examples/)

### 4.5 自訂告警 `_custom_alerts`

讓租戶用平台預先定義的 **recipe** 宣告自己的告警，**完全不需要寫 PromQL**。租戶在自己的 YAML 裡填參數，平台編譯成實際的 rule。

```yaml
tenants:
  db-b:
    _custom_alerts:
      - recipe: threshold              # recipe 種類
        name: high_connections         # 同租戶內唯一
        metric: mysql_global_status_threads_connected
        op: ">"
        window: 5m
        threshold: "150:warning"       # 值 + 可選 :severity
        mode: page                     # page=通知 / silent=只進 dashboard
        for: 1m                        # 持續多久才觸發（enum-bounded）
```

可用 recipe（填的參數依 recipe 不同）：

| Recipe | 用途 |
|--------|------|
| `threshold` | 數值跨過閾值 |
| `rate` | 變化率超過閾值 |
| `ratio` | 兩個 metric 的比值超過閾值（需 `denominator_metric`） |
| `absence` | metric 在窗口內消失（不需 threshold） |
| `p99_latency` | p99 延遲超過閾值 |
| `forecast` | 線性預測在 `horizon` 內會跨過閾值（容量耗盡預警） |
| `slo_burn_rate` | SLO burn-rate：宣告 `objective` 即編譯出 fast(critical)/slow(warning) 雙檔多窗告警（需 `denominator_metric`；[ADR-031](../../docs/adr/031-slo-burn-rate-recipe.md)） |

要點：

- 嚴重度用 `threshold: "值:severity"`（`warning` / `critical`，省略為 warning）。
- `slo_burn_rate` 例外：**不填 `threshold`、不填 `window`**，改填 `objective: "99.9"`（SLO 目標百分比，字串、(0,100) 開區間；`"disable"` 三態關閉）；severity 由 recipe 固定（fast→critical、slow→warning）。選用 `slo_period: 28d|30d`（預設 30d，只影響倍率、改值不換 shape）與 `min_events`（預設 10，fast 短窗壞事件絕對數下限——低流量防誤報樓層）。
- 選用 `selectors:`（精確）/ `selectors_re:`（regex）加 label 過濾；保留 label（`tenant` / `severity` / `__name__` 等）不可用。
- 輸出為 `user_threshold{component="custom", …}`；解析失敗的項目會被丟棄並計入 `da_custom_alert_parse_errors`（fail-loud）。
- 每租戶有 recipe 數量上限（成本護欄；`slo_burn_rate` 一條宣告展開 critical+warning 兩個 severity、計 2）。

> 用 portal 的 Recipe Builder 可用表單產生上面這段 YAML，不必手寫（`slo_burn_rate` 表單精靈規劃中，目前走 YAML）。完整 recipe 參數與生命週期見 [架構與設計](../../docs/architecture-and-design.md)、[ADR-024](../../docs/adr/024-version-aware-threshold-via-dimensional-label.md) 及 [ADR-031](../../docs/adr/031-slo-burn-rate-recipe.md)（slo_burn_rate）。

---

## 5. 配套 CLI

三隻 CLI 都從 `app/cmd/<binary>` build，與 runtime 共用 `internal/` + `pkg/config`，確保「CLI 行為」與「線上行為」不漂移。

### `da-guard` — `_defaults.yaml` 的 pre-merge 守門員

在 CI / pre-commit 階段攔 schema / routing / cardinality / 冗餘 override 問題，不讓壞改動進到線上。

routing 檢查的是租戶**解析後**的 routing（[#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)）：根目錄 `_routing_defaults` → `_routing_profile` 指向的 profile → 租戶 `_routing`，與 route generator 同一套合併（共用 `app/pkg/routingpolicy`，以 `tests/shared/routing_policy_parity_matrix.json` 與 Python 端對齊）。主 receiver、`overrides`、ADR-007 `routes` 都做形狀檢查（規則在 `app/pkg/receiverspec`，與 tenant-api 共用；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295) 起也查 `send_resolved`／`require_tls` 的布林值與 `http_config`），並依 `_domain_policy.yaml` 判 receiver type（forbidden 與 allowed 分開判）。租戶那一層只讀租戶檔與根目錄平台檔 `tenants.<id>` 的 `_routing` / `_routing_profile`（[#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)），寫在 defaults 區塊或 threshold profile 裡的 routing 由 `routing_in_unread_location` 點名。新增的 finding kind 與嚴重度見 [cli-reference §guard](../../docs/cli-reference.md#guard)。

```bash
da-guard --config-dir conf.d/ --required-fields cpu,memory   # 上限取根 _defaults.yaml 的 max_metrics_per_tenant（未設 = 500）
da-guard --config-dir conf.d/ --format json --output guard-report.json
```

GitHub Actions 範本：[`guard-defaults-impact.yml`](../../.github/workflows/guard-defaults-impact.yml)——客戶可整份 copy，於 `**/_defaults.yaml` 變更時自動跑並貼 PR comment。

子命令 `served-values`（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）以 JSON 印出 `/metrics` 對每個租戶實際發出的值，值由 exporter 自己的載入與解析算出；Python 讀取端經 `scripts/tools/_lib_tenant_values.py` 呼叫。輸出欄位與 exit code 見 [cli-reference §guard](../../docs/cli-reference.md#guard)。

```bash
da-guard served-values --config-dir conf.d/ --at 2026-07-01T03:00:00Z
```

子命令 `effective`（[#2564](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2564)）以 JSON 印出每個租戶在 tenant-api `/effective` 的答案（同一個 `pkg/config` resolver），另加綁定的 profile 與每個 key 的來源層級與檔案；Python 讀取端經 `scripts/tools/_lib_tenant_values.py` 的 `load_effective()` 呼叫。輸出欄位與 exit code 見 [cli-reference §guard](../../docs/cli-reference.md#guard)。

```bash
da-guard effective --config-dir conf.d/
```

### `da-parser` — kube-prometheus 規則 → ParseResult JSON

導入既有 PrometheusRule 的第一步：解析、dialect 分類（標準 PromQL / VictoriaMetrics-only）、可選 portability gate。

```bash
da-parser import --input rules.yaml --output rules.json
da-parser import --input rules.yaml --fail-on-non-portable
da-parser allowlist                 # 印出 VM-only allowlist（introspection）
```

### `da-batchpr` — 階層感知的 Batch PR 管線

JSON-in / JSON-out + Markdown report；上游由 Python `da-tools` 包裝呼叫。

| Subcommand | 作用 |
|-----------|------|
| `apply` | 依 plan 開 / 更新各租戶的 chunk PR |
| `refresh` | Base PR merge 後，把租戶分支 rebase 到新的 main HEAD |
| `refresh-source` | 把 data-layer hot-fix 重新 apply 到既有租戶分支 |

> **Python 包裝**：`da-tools guard defaults-impact` / `da-tools batchpr *` 會 shell-out 到對應 Go binary。Binary 解析序：`--<bin>-binary` 旗標 → `$DA_<BIN>_BINARY` 環境變數 → `$PATH`。

---

## 6. 部署

用 [`helm/threshold-exporter/`](../../helm/threshold-exporter/) 部署（詳見該目錄 README）。Chart 會建立 Deployment（多副本 + PDB）/ Service / ConfigMap；config **完全由 ConfigMap volume 注入，Docker image 不含任何 config 檔**。

```bash
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.9.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

GitOps / kubectl-patch / da-tools 三種注入 ConfigMap 的方式見 [`docs/integration/gitops-deployment.md`](../../docs/integration/gitops-deployment.md)。

### 熱重載模型

ConfigMap 變更 → K8s 在 1–2 分鐘內 propagate 到 Pod volume → exporter 下個 tick（`-reload-interval`，預設 30s）偵測 `merged_hash` 變化 → 套 debounce → atomic-swap config + 累加 `da_config_reload_trigger_total{reason}`。**全程不重啟 Pod。**

---

## 7. 開發

| Make target | 用途 |
|-------------|------|
| `make component-build COMP=threshold-exporter` | Build Go binary + 載入 Kind |
| `make component-deploy COMP=threshold-exporter ENV=local` | 部署 + 注入測試租戶 |
| `make dc-go-test` | Dev container 內跑 Go tests（race + count=1） |
| `make benchmark-report` | benchmark 套組 |
| `make pre-tag` | ⛔ 打 tag 前必跑 |
| `make pr-preflight` | ⛔ PR merge 前必跑 |

本機快速 build + 跑見 [QUICKSTART.md](QUICKSTART.md)。改閾值用 `python3 scripts/tools/patch_config.py <tenant> <metric> <value>`（自動偵測單檔 / 多檔模式），exporter 下個 reload tick 自動載入。

驗證部署：

```bash
kubectl port-forward svc/threshold-exporter 8080:8080 -n monitoring &
curl -s http://localhost:8080/metrics | grep user_threshold
curl -s http://localhost:8080/api/v1/config                      # resolved view
curl -s -XPOST http://localhost:8080/api/v1/tenants/simulate \
  -H 'Content-Type: application/json' -d @simulate-payload.json   # 合併預覽
```

`/simulate` 回 400 而 `{error}` 寫著 `the exporter would skip this …` 時，代表同一份內容 commit 進 conf.d 也不會生效（exporter 會把整份檔記進 `parse_failed`）——修正點名的那份檔再重試。

---

> **回報問題** — [Issue tracker](https://github.com/vencil/Dynamic-Alerting-Integrations/issues)。若是熱重載 / debounce / merged_hash 相關，請附 `da_config_reload_trigger_total` 與 `da_config_blast_radius_tenants_affected` 連續 5 分鐘的 scrape 樣本。
