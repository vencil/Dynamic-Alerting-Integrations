# Threshold Exporter (v2.7.0)

<!-- 標題版號 = 最後 released tag；v2.8.0 in-flight feature 在內文以 **v2.8.0** inline 標記。
     Phase .e release wrap 切五線 tag 時，本標題 + 下方 helm --version 跟著批次同步 bump。 -->

> **核心 component** — 把 `conf.d/` YAML 配置轉成 Prometheus `user_threshold` 系列 metrics 的 config-driven exporter。Directory Scanner + 四層繼承 + Dual-Hash 熱重載 + Cardinality Guard。
>
> **Companion 文件：** [helm chart](../../helm/threshold-exporter/) · [architecture-and-design](../../docs/architecture-and-design.md) · [migration-guide](../../docs/migration-guide.md) · [rule-packs](../../rule-packs/README.md)

---

## 1. What & Why

- **Input** — `conf.d/*.yaml`（單檔扁平 _或_ `<domain>/<region>/<tenant>.yaml` 階層），ConfigMap volume mount 在 runtime 注入
- **Output** — Prometheus gauge `user_threshold{tenant, component, metric, severity, ...}` + 四個運營狀態 gauge + 十個 reload-side metrics
- **Why config-driven** — 1000+ 租戶場景下避免「修閾值要改 PromQL recording rule + 重啟 Prometheus」；YAML 改動 K8s ConfigMap propagate 後 < 30s 自動 hot-reload，**不掉 scrape**
- **不做的事** — 不執行 PromQL（只輸出 threshold gauge）；不做 alerting routing（交給 Alertmanager）；不持久化（無狀態 + ConfigMap 是 SSOT）

> **架構深度** — 9 個核心設計概念（Severity Dedup / Sentinel Alert / 四層路由 / Dual-Perspective / Tenant API ...）見 [architecture-and-design.md §設計概念總覽](../../docs/architecture-and-design.md#設計概念總覽)。本 README 只負責 operator quick-reference。

---

## 2. What's New in v2.8.0

| # | 能力 | 影響 |
|---|------|------|
| 1 | **客戶導入管線** — 三隻新 CLI：`da-parser`（PrometheusRule → ParseResult JSON）、`da-batchpr`（Hierarchy-aware Batch PR with apply / refresh / refresh-source 三 mode）、`da-guard`（pre-merge gate for `_defaults.yaml`） | 從「現有客戶手動寫 conf.d」到「kube-prometheus 客戶 onboarding 全自動化」，C-8 / C-9 / C-10 / C-12 軌道 |
| 2 | **`/api/v1/tenants/simulate` ephemeral primitive** — POST 帶 base64 tenant.yaml + defaults chain，回傳 `merged_hash` + `effective_config` + 完整 inheritance preview。**無 disk IO，無 manager state mutation** | C-7b：CI 與 simulator UI 在 commit 前可預測 inheritance 影響；蓋過 `da-guard` 的 speculative 缺口 |
| 3 | **Issue #61 metric 拆分 + Blast-Radius Histogram** — `da_config_defaults_change_noop_total` 收斂為純 cosmetic edits；`da_config_defaults_shadowed_total` 為新 counter 抓「被 tenant override 擋下」；`da_config_blast_radius_tenants_affected{reason, scope, effect}` 為新 histogram 量化每次 tick 受影響 tenant 分佈 | 既有 dashboard 用舊 noop counter 衡量「inheritance 擋下多少」需切到 shadowed counter |
| 4 | **Mixed-mode duplicate tenant 從 WARN → hard error** — 同 tenant id 同時出現在 flat + nested 路徑，`Load()` 直接拒絕 + 保留 `m.config` 為 nil（cold-start）或 prior known-good（hot-reload） | Breaking：先前 silently last-wins 的部署會在 v2.8.0 升級時 fail-loudly。詳 [issue #127](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/127) |
| 5 | **ZH-primary policy lock** — 文件 SSOT 鎖中文，`foo.md`(ZH) + `foo.en.md`(EN) 雙寫；不執行 v2.5.0 規劃的 ZH→EN 遷移（[planning S#101](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/101)） | 本 README 含 codebase 內 dev rules 文件均為 ZH primary |

> **升級路徑** — v2.7.0 → v2.8.0 升級風險點與 mitigation 見 [migration-guide.md](../../docs/migration-guide.md)。

---

## 3. Operator Reference

### Endpoints

| Path | Method | 用途 | 引入版本 |
|------|--------|------|---------|
| `/metrics` | GET | Prometheus scrape | v0.1.0 |
| `/health` | GET | Liveness probe | v0.1.0 |
| `/ready` | GET | Readiness probe（`config_loaded` 才回 200） | v0.1.0 |
| `/api/v1/config` | GET | Resolved config + tenant list（debug；支援 `?at=<RFC3339>` 模擬未來時間點） | v1.0.0 |
| `/api/v1/tenants/simulate` | POST | Ephemeral merge preview（不寫狀態） | **v2.8.0** |

### Flags / Env

| Flag | Env | Default | 說明 |
|------|-----|---------|------|
| `-config-dir` | `CONFIG_DIR` | (auto) | conf.d 目錄路徑（推薦，takes precedence） |
| `-config` | `CONFIG_PATH` | (auto) | 單檔 legacy 模式 |
| `-listen` | `LISTEN_ADDR` | `:8080` | HTTP listen address |
| `-reload-interval` | — | `30s` | watch tick 間隔 |
| `-scan-debounce` | — | `300ms` | fsnotify burst coalesce window（0 停用，回 v2.6.x 行為） |

> **Auto-detect 規則**：`-config-dir` 優先；其次 `-config`；最後依序試 `/etc/threshold-exporter/conf.d/` → `/etc/threshold-exporter/config.yaml`。

### Metrics

**Threshold-domain（dynamic labels）：**

| Metric | Type | 用途 |
|--------|------|------|
| `user_threshold` | Gauge | resolved threshold（labels: tenant / component / metric / severity / 維度 labels） |
| `user_silent_mode` | Gauge | silent mode active（label: tenant / target_severity） |
| `tenant_metadata_info` | Gauge | metadata 注入用（值恆 1，labels: runbook_url / owner / tier / ...） |
| `da_config_event` | Gauge | timed config 失效事件（silent / maintenance auto-deactivated） |

**Reload / config-domain（observability）：**

| Metric | Type | 用途 |
|--------|------|------|
| `da_config_scan_duration_seconds` | Histogram | full dir scan 耗時 |
| `da_config_reload_duration_seconds` | Histogram | 完整 reload 耗時（scan + parse + merge + commit） |
| `da_config_reload_trigger_total{reason}` | Counter | reasons: `source` / `defaults` / `new_tenant` / `delete` / `forced` |
| `da_config_debounce_batch_size` | Histogram | 每次 fire 吸收的 trigger 數（debounce 健康指標） |
| `da_config_parse_failure_total` | Counter | YAML parse / boundary 違規次數 |
| `da_config_defaults_change_noop_total` | Counter | cosmetic edits（v2.8.0 收斂語義；舊 dashboard 注意） |
| `da_config_defaults_shadowed_total` | Counter | **v2.8.0** — defaults 變更被 tenant override 擋下 |
| `da_config_blast_radius_tenants_affected{reason, scope, effect}` | Histogram | **v2.8.0** — 每次 tick 受影響 tenant 分佈 |
| `da_config_last_scan_complete_unixtime_seconds` | Gauge | 上次 scan 結束時間（age = now - 此值） |
| `da_config_last_reload_complete_unixtime_seconds` | Gauge | 上次 reload 結束時間 |

### Exit Codes（CLI binaries）

| Code | `da-guard` | `da-parser` | `da-batchpr` |
|------|-----------|-------------|--------------|
| 0 | clean | parse OK | all targets succeeded |
| 1 | error finding（block merge） | gate failure（non-portable / ambiguous） | one or more targets failed |
| 2 | caller error（flag / path） | caller error | caller error |

---

## 4. Config Reference

### 邊界規則

| 檔案 pattern | 允許區塊 | 違規行為 |
|-------------|---------|---------|
| `_*.yaml`（如 `_defaults.yaml`） | `defaults` / `state_filters` / `tenants`（但通常只放 defaults） | — |
| `<tenant>.yaml` | 僅 `tenants`（含其子鍵 `_metadata` / `_silent_mode` / `_state_maintenance` / `_severity_dedup`） | 其他區塊自動忽略 + WARN log |

### 四層繼承（ADR-018, v2.7.0+）

```
conf.d/
├── _defaults.yaml             L0 平台預設
├── mysql/
│   ├── _defaults.yaml         L1 domain 預設
│   ├── us-east/
│   │   ├── _defaults.yaml     L2 region 預設
│   │   └── db-a.yaml          L3 tenant override
```

合併語義：**deep merge**（map 遞迴）+ **array 替換**（list 整包覆寫，不串接）+ **null-as-delete**（下層設 `null` 等同顯式否決）+ **保留前綴**（`_state_*` / `_routing*` / `_metadata` 只允許 `_` 前綴檔案）。

### 三態 + 嚴重度

| 設定 | Prometheus 輸出 |
|------|----------------|
| `"70"` | `user_threshold{...} 70`（severity=warning） |
| `"40:critical"` | `user_threshold{...,severity="critical"} 40` |
| 省略不寫 | 套 default value |
| `"disable"` | 不產生 metric |

### Grammar quick-table

| 形式 | 範例 key | 範例 value | 備註 |
|------|---------|-----------|------|
| 純量閾值 | `mysql_connections` | `"70"` | 最常見 |
| 維度標籤（exact） | `"redis_queue_length{queue='tasks'}"` | `"500:critical"` | YAML key 必加引號；不繼承 defaults |
| 維度標籤（regex） | `"oracle_tablespace{tablespace=~'SYS.*'}"` | `"95"` | 輸出 `tablespace_re` label，匹配交 PromQL `label_replace` |
| 排程式閾值 | `mysql_connections:` | `default + overrides[]` | UTC `HH:MM-HH:MM` 窗口；多窗口 first-match-wins |
| 租戶 metadata | `_metadata` | `{runbook_url, owner, tier, env, region, domain, db_type, ...}` | 注入 `tenant_metadata_info` gauge |
| Silent mode | `_silent_mode` | `"warning"` 或 `{target, expires, reason}` | `expires` ISO 8601 自動失效 |
| Maintenance | `_state_maintenance` | `{target, expires, reason, recurring[]}` | `recurring` 由 `da-tools maintenance-scheduler` CronJob 執行 |
| Severity dedup | `_severity_dedup` | `true` | Critical 觸發時抑制 Warning 通知 |

完整範例：

- **單一 DB（MariaDB）** — [`config/conf.d/db-a.yaml`](config/conf.d/db-a.yaml)
- **多 DB 維度** — [`config/conf.d/examples/`](config/conf.d/examples/)（Redis / MongoDB / Elasticsearch + `_defaults-multidb.yaml` + `_routing_profiles.yaml` + `_domain_policy.yaml` + `_instance_mapping.yaml`）

> **語法細節** — dimensional labels / regex labels / scheduled overrides / metadata 完整規則見 [migration-guide.md](../../docs/migration-guide.md)；recording rule 適配見 [migration-guide.md §平台團隊 PromQL 適配](../../docs/migration-guide.md#平台團隊-promql-適配-重要)。

---

## 5. Companion CLIs

三隻 CLI 都從 `components/threshold-exporter/app/cmd/<binary>` build，共用 `internal/` 與 `pkg/config` library，避免「CLI 行為」與「runtime 行為」漂移。

### `da-guard` — pre-merge gate for `_defaults.yaml`

CI / pre-commit 階段攔截 schema / routing / cardinality / redundant override 四層問題，不讓壞改動 reach WatchLoop。

```bash
da-guard --config-dir conf.d/ --required-fields cpu,memory --cardinality-limit 500
da-guard --config-dir conf.d/ --format json --output guard-report.json
```

GitHub Actions template：[`/.github/workflows/guard-defaults-impact.yml`](../../.github/workflows/guard-defaults-impact.yml) — 客戶可整份 copy，`pull_request` 觸發於 `**/_defaults.yaml` 變更時自動跑、posting sticky PR comment、artifact 留 14 天。

### `da-parser` — kube-prometheus PrometheusRule → ParseResult JSON

導入既有 PrometheusRule corpus 的第一步：解析、dialect 分類（PromQL strict / VictoriaMetrics-only）、可選 `--fail-on-non-portable` gate。

```bash
da-parser import --input rules.yaml --output rules.json
da-parser import --input rules.yaml --validate-strict-prom --fail-on-non-portable
da-parser allowlist                # 印出 VM-only allowlist（introspection）
```

### `da-batchpr` — hierarchy-aware Batch PR pipeline

JSON-input-first contract（每個 subcommand 讀 JSON 寫 JSON + Markdown report），smart-parts 留給上游 Python `da-tools`。

| Subcommand | 作用 |
|-----------|------|
| `apply` | 從 C-9 emit + C-10 BuildPlan 開 / update tenant chunk PRs |
| `refresh` | Base PR merged 後，rebase tenant branches 到新 main HEAD |
| `refresh-source` | 把 data-layer hot-fix 重新 apply 到既有 tenant branches |

> **Python 包裝** — `da-tools guard defaults-impact` 與 `da-tools batchpr *` 透過 `scripts/tools/ops/guard_dispatch.py` shell-out 到對應 Go binary。Binary 解析序：`--<bin>-binary` flag → `$DA_<BIN>_BINARY` env → `$PATH`。

---

## 6. Deploy

部署用 [`helm/threshold-exporter/`](../../helm/threshold-exporter/) — 詳見該目錄 README。Chart 自動建立 Deployment (2 replicas + PDB) / Service / ConfigMap (`threshold-config`)；config 完全 ConfigMap volume mount 注入，**Docker image 不含任何 config 檔案**。

Quick-start：

```bash
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.7.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

GitOps / kubectl-patch / da-tools 注入 ConfigMap 三種方式詳見 [`docs/integration/gitops-deployment.md`](../../docs/integration/gitops-deployment.md)。

### Hot-reload 模型

ConfigMap 變更 → K8s 在 1-2 分鐘內 propagate 至 Pod volume → exporter `-reload-interval`（預設 30s）下個 tick 偵測 `merged_hash` 變化 → 套 300ms debounce → atomic-swap config + emit `da_config_reload_trigger_total{reason}`。**不需重啟 Pod。**

---

## 7. Develop

| Make target | 用途 |
|-------------|------|
| `make component-build COMP=threshold-exporter` | Build Go binary + load 進 Kind |
| `make component-deploy COMP=threshold-exporter ENV=local` | 部署 + 注入 db-a / db-b 測試租戶 |
| `make dc-go-test` | Dev container 內跑 Go tests（race + count=1） |
| `make benchmark-report` | 17 benches × count=6（含 4 mixed-mode） |
| `make pre-tag` | ⛔ 打 tag 前必跑（version-check + lint-docs + benchmark gate） |
| `make pr-preflight` | ⛔ PR merge 前必跑（七項檢查 + `.git/.preflight-ok.<SHA>` marker） |

修改閾值用 `python3 scripts/tools/patch_config.py <tenant> <metric> <value>`（自動偵測單檔/多檔模式 + 安全更新）；exporter 在下個 reload tick 自動載入。

驗證部署：

```bash
kubectl port-forward svc/threshold-exporter 8080:8080 -n monitoring &
curl -s http://localhost:8080/metrics | grep user_threshold
curl -s http://localhost:8080/api/v1/config        # resolved view
curl -s -XPOST http://localhost:8080/api/v1/tenants/simulate \
  -H 'Content-Type: application/json' -d @simulate-payload.json
```

---

> **回報問題** — Issue tracker：https://github.com/vencil/Dynamic-Alerting-Integrations/issues。若是 hot-reload / debounce / merged_hash 行為，請附 `da_config_reload_trigger_total` + `da_config_blast_radius_tenants_affected` 連續 5 分鐘的 scrape 樣本。
