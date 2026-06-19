# Threshold Exporter (v2.9.0)

<!-- 標題版號 = 最後 released tag。Release wrap 切五線 tag 時，本標題 + §Deploy 的 helm --version 跟著批次同步 bump。 -->

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
| **輸入** | `conf.d/*.yaml`——單檔扁平 _或_ `<domain>/<region>/<tenant>.yaml` 階層；由 K8s ConfigMap volume 在 runtime 注入 |
| **輸出** | Prometheus gauge `user_threshold{tenant, component, metric, severity, …}` + 數個營運狀態 gauge + 一組 reload 觀測 metrics |
| **解決的問題** | 千租戶場景下，避免「改一個閾值要動 PromQL recording rule + 重啟 Prometheus」 |
| **不做的事** | 不執行 PromQL（只輸出 threshold gauge）· 不做告警路由（交給 Alertmanager）· 不持久化（無狀態，ConfigMap 是唯一真實來源） |

> 9 個核心設計概念（Severity Dedup / Sentinel Alert / 四層路由 / Dual-Perspective / Tenant API …）見 [架構與設計 §設計概念總覽](../../docs/architecture-and-design.md#設計概念總覽)。本 README 只負責營運層的 quick-reference。

---

## 2. 核心能力

| 能力 | 一句話 |
|------|--------|
| **Config-driven 閾值** | YAML 數字直接變 `user_threshold` metric；三態（自訂值 / 套預設 / 停用）+ 嚴重度後綴 |
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
| `/metrics` | GET | Prometheus scrape（Go runtime metrics + 本 exporter 自訂 collector） |
| `/health` | GET | Liveness probe（process 起來即 200） |
| `/ready` | GET | Readiness probe（config 載入完成才回 200，否則 503） |
| `/api/v1/config` | GET | Resolved config + 租戶清單（debug；支援 `?at=<RFC3339>` 模擬未來時間點） |
| `/api/v1/tenants/simulate` | POST | Ephemeral 合併預覽——帶 base64 的 tenant YAML + defaults chain，回傳 `merged_hash` + 完整 inheritance 預覽。**不寫 disk、不改 manager 狀態** |

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
| `tenant_metadata_info` | Gauge | metadata 注入用，值恆 1（labels: runbook_url / owner / tier） |
| `tenant_expected_exporter` | Gauge | per-tenant exporter liveness 期望，值恆 1（labels: tenant / db_type；**僅對宣告 `_metadata.db_type` 的租戶 emit**）。`TenantExporterAbsent` anti-join 的左手邊（#869） |
| `da_config_event` | Gauge | timed config 失效事件（silent / maintenance 自動解除） |
| `da_custom_alert_parse_errors` | Gauge | 每租戶被丟棄的 `_custom_alerts` 數（fail-loud；0 = 全數有效） |
| `da_tenant_metrics_over_limit` | Gauge | 每租戶超出 cardinality 上限的量（`max(0, 產出數 − 上限)`；持續超限就持續報該值） |

**營運域**（觀測 exporter 自身的熱重載健康）：

| Metric | Type | 用途 |
|--------|------|------|
| `da_config_reload_trigger_total{reason}` | Counter | reload 次數，reason: `source` / `defaults` / `new` / `delete` / `forced` |
| `da_config_reload_duration_seconds` | Histogram | 完整 reload 耗時（scan + parse + merge + commit） |
| `da_config_scan_duration_seconds` | Histogram | 目錄掃描耗時 |
| `da_config_debounce_batch_size` | Histogram | 每次 fire 吸收的 trigger 數（debounce 健康指標） |
| `da_config_parse_failure_total{file_basename}` | Counter | YAML parse / 邊界違規次數（定位壞檔） |
| `da_config_defaults_change_noop_total` | Counter | 純 cosmetic 的 `_defaults` 變更（註解 / 排序，無實質影響） |
| `da_config_defaults_shadowed_total` | Counter | `_defaults` 變更被租戶 override 擋下的數量 |
| `da_config_blast_radius_tenants_affected{reason,scope,effect}` | Histogram | 每次 tick 受影響租戶的分佈 |
| `da_config_last_scan_complete_unixtime_seconds` | Gauge | 上次掃描完成時間（`time() − 此值` = 卡住偵測） |
| `da_config_last_reload_complete_unixtime_seconds` | Gauge | 上次 reload 完成時間 |
| `da_config_free_os_memory_total` | Counter | 主動還記憶體給 OS 的次數（未開 `-free-os-mem-after-reload` 時恆 0） |

### 3.4 Exit Codes（CLI binaries）

| Code | `da-guard` | `da-parser` | `da-batchpr` |
|------|-----------|-------------|--------------|
| 0 | clean | parse OK | 全部目標成功 |
| 1 | 發現錯誤（擋 merge） | gate 失敗（non-portable / ambiguous） | 一個以上目標失敗 |
| 2 | caller error（旗標 / 路徑） | caller error | caller error |

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

### 4.3 三態 + 嚴重度

| 設定 | Prometheus 輸出 |
|------|----------------|
| `"70"` | `user_threshold{…} 70`（severity=warning） |
| `"40:critical"` | `user_threshold{…,severity="critical"} 40` |
| 省略不寫 | 套繼承來的 default |
| `"disable"` | 不產生 metric |

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
| Severity dedup | `_severity_dedup` | `true` | critical 觸發時抑制 warning 通知 |
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

要點：

- 嚴重度用 `threshold: "值:severity"`（`warning` / `critical`，省略為 warning）。
- 選用 `selectors:`（精確）/ `selectors_re:`（regex）加 label 過濾；保留 label（`tenant` / `severity` / `__name__` 等）不可用。
- 輸出為 `user_threshold{component="custom", …}`；解析失敗的項目會被丟棄並計入 `da_custom_alert_parse_errors`（fail-loud）。
- 每租戶有 recipe 數量上限（成本護欄）。

> 用 portal 的 Recipe Builder 可用表單產生上面這段 YAML，不必手寫。完整 recipe 參數與生命週期見 [架構與設計](../../docs/architecture-and-design.md) 及 [ADR-024](../../docs/adr/024-version-aware-threshold-via-dimensional-label.md)。

---

## 5. 配套 CLI

三隻 CLI 都從 `app/cmd/<binary>` build，與 runtime 共用 `internal/` + `pkg/config`，確保「CLI 行為」與「線上行為」不漂移。

### `da-guard` — `_defaults.yaml` 的 pre-merge 守門員

在 CI / pre-commit 階段攔 schema / routing / cardinality / 冗餘 override 問題，不讓壞改動進到線上。

```bash
da-guard --config-dir conf.d/ --required-fields cpu,memory --cardinality-limit 500
da-guard --config-dir conf.d/ --format json --output guard-report.json
```

GitHub Actions 範本：[`guard-defaults-impact.yml`](../../.github/workflows/guard-defaults-impact.yml)——客戶可整份 copy，於 `**/_defaults.yaml` 變更時自動跑並貼 PR comment。

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

---

> **回報問題** — [Issue tracker](https://github.com/vencil/Dynamic-Alerting-Integrations/issues)。若是熱重載 / debounce / merged_hash 相關，請附 `da_config_reload_trigger_total` 與 `da_config_blast_radius_tenants_affected` 連續 5 分鐘的 scrape 樣本。
