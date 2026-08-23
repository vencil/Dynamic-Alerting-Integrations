---
title: "ADR-017: _defaults.yaml 繼承語意 + dual-hash hot-reload"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-017
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-04-18
updated_at: 2026-08-23
---
# ADR-017: _defaults.yaml 繼承語意 + dual-hash hot-reload

> **Language / 語言：** **中文 (Current)** | [English](./017-defaults-yaml-inheritance-dual-hash.en.md)

> v2.7.0 Scale Foundation 第二塊。與 [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)（目錄分層）為一組。

## 狀態

✅ **Accepted**（v2.7.0, 2026-04-19）— 多層 `_defaults.yaml` 繼承 + dual-hash 熱重載 + 300ms debounce 已隨 v2.7.0 出貨；noop 語義拆分（`shadowed` / `cosmetic`）為 v2.8.0 amendment。

## 背景

v2.6.x 的 `_defaults.yaml` 僅在 flat `conf.d/` 根目錄存在一份全局 defaults。
引入 ADR-016 的分層目錄後，需要定義多層 `_defaults.yaml` 的繼承語意：

- 哪些層級可以放 `_defaults.yaml`？
- 父子層 defaults 如何 merge？
- `_defaults.yaml` 變動時，哪些 tenant 需要 reload？如何避免 reload 風暴？

v2.5.0 已有 SHA-256 hot-reload（`source_hash` 比對），但只追蹤 tenant YAML 本身。
現在 tenant 的 **effective config** 同時取決於自身 YAML + 繼承的 defaults，
需要第二層 hash 來判斷「effective config 是否真的變了」。

## 決策

### 繼承層級

`_defaults.yaml` 可出現在以下任意層級（皆為選填）：

```
conf.d/
├── _defaults.yaml              ← L0: 全局 defaults
├── {domain}/
│   ├── _defaults.yaml          ← L1: domain-level defaults
│   └── {region}/
│       ├── _defaults.yaml      ← L2: region-level defaults（少見）
│       └── {env}/
│           ├── _defaults.yaml  ← L3: env-level defaults
│           └── tenant-001.yaml
```

繼承順序：**L0 → L1 → L2 → L3 → tenant YAML**（後者覆蓋前者）。

### Merge 語意：Deep Merge with Override

- **Dict/Map 欄位**：deep merge（子層新增的 key 會保留，相同 key 子層覆蓋父層）
- **Array/List 欄位**：**replace，不 concat**（避免語意歧義 — "我覆蓋了 group_by，怎麼多出舊值？"）
  - ⚠️ **唯一例外：`_custom_alerts` 走 UNION**（ADR-024 / #772）——租戶自己的清單**加到**
    繼承來的平台 / domain policy recipe 上，不取代它們（`describe_tenant.py` 在 deep_merge
    之後覆寫該鍵）。
- **Scalar 欄位**：子層覆蓋父層
- **Null 值 — 依欄位而分，不是通則**（#1339 拆開原本合寫的一行）：
  - **路由的四個欄位**（`_routing` 底下的 `group_by` / `group_wait` /
    `group_interval` / `repeat_interval`）：顯式 `null` **退出繼承**，產出的 route
    省略該欄位。這是唯一的表達方式——這些欄位沒有 `"disable"` 哨兵值。（⚠️ `group_by` 並不是
    時間欄位；且 `group_by: []` 同樣會讓該欄位被省略——`_grar_routes.py` 用的是 falsy 檢查
    `if group_by and isinstance(group_by, list)`，不是 `is not None`。）
    `_routing.receiver` 與 `_routing.overrides` **不適用**：前者會讓該租戶整條
    route 消失（告警落到 catch-all），後者無上層可退。
  - **閾值 key**：顯式 `null` **不退出繼承**，請改用 `"disable"`。發射面
    （`collector.go` → `ResolveAtWithStats`）本來就會忽略 null 並回退平台預設；
    診斷面（`/effective`、`describe_tenant`、simulate）已於 #1339 對齊。
  - **其餘 `_` 前綴的保留 key**（`_silent_mode` / `_profile` / `_severity_dedup` /
    `_namespaces` 等）：顯式 `null` **退出繼承**——`pkg/config/hierarchy.go` 的 `deepMerge`
    對任何 `_` 前綴鍵的 explicit null 做 `delete(result, k)`，非 `_` 前綴（＝閾值鍵）只
    `continue`。⚠️ 該處註解把本 ADR 指為這條規則的權威，所以規則寫在這裡：**判準是「是否
    `_` 前綴」，不是「是否路由欄位」。**
- **⚠️ 「空值」與顯式 `null` 是同一件事**——原文把兩者並列成「Null / 空值」，
  正是誤導的來源：`mysql_connections: ~` 與 `mysql_connections:` 語法不同，但
  YAML 解析出來**都是 null**。所以若讓閾值面的 null 生效，等於明文規定
  「打到一半忘了填」＝安靜關掉一條告警。這就是閾值面不支援 null 的理由：
  意外要走向**吵**，不能走向**靜**。
- **`_metadata` 欄位不繼承**：每個 tenant 的 `_metadata` 僅來自自身 YAML + 路徑推斷（ADR-016）

```yaml
# L0 _defaults.yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30

# ↓ 頂層 key，與 `defaults:` 平級 —— 不是巢狀在它底下。
#   `defaults:` 的型別是 map[string]float64，塞任何巢狀 mapping 進去會讓
#   **整份檔案**解析失敗 ⇒ 連同所有預設值一起被丟棄，不是只丟那一個 key。
#   兩個消費端都會 log ERROR（exporter 走 parsePartialConfig，另加
#   parse_failure metric；tenant-api 走 merge_tenant.go），但**都不會**套用
#   任何預設值。
_routing_defaults:
  group_wait: "60s"
  group_interval: "5m"

# L1 finance/_defaults.yaml
defaults:
  pg_stat_activity_count: 200     # override: 金融 domain 更嚴格
  pg_locks_count: 100             # 新增: domain-specific

# tenant YAML
tenants:
  fin-db-001:
    pg_stat_activity_count: "150" # override: 單一 tenant 最嚴格
                                  # ⚠️ 這裡加引號、上面 `defaults:` 不加 ——
                                  # 租戶值是 ScheduledValue（字串｜物件），
                                  # 平台預設是 map[string]float64
    # pg_replication_lag_seconds: 繼承 L0 = 30
    # pg_locks_count: 繼承 L1 = 100
    # _routing_defaults.group_wait: 由四層 routing 引擎繼承 = 60s
    #   ⛔ 但它不在下面那個 effective config 裡 —— 見緊接著的範圍註記
```

**Effective config 計算**：
```
effective = deep_merge(L0, L1, L2, L3, tenant_yaml)
```

⛔ **範圍：`effective` 只取 `_defaults.yaml` 的 `defaults:` 區塊**，不取
[`platform-defaults.schema.json`](../schemas/platform-defaults.schema.json) 中**與 `defaults:`
平級的**其他頂層 properties（`state_filters` / `optional_overrides` / `profiles` /
`max_metrics_per_tenant` / `_routing*` 等）。⛔ 它們是 `defaults:` 的**兄弟鍵，不是它底下的
欄位**——縮排進 `defaults:` 會讓整份檔案解析失敗（見上方 worked example 的警告）。
**權威清單是那份 schema，勿在此處另立列舉。** 兩個 unwrap 實作在這個
形狀上同義：`describe_tenant.py` 的 `ddata.get("defaults", ddata)`、Go 的
`pkg/config.extractDefaultsBlock`。

⚠️ **目前已知的可達例外（非窮舉——這份清單本身不是保證）。合併行為是刻意的，但後果各有未結的票**：

1. **沒有 `defaults:` 包裝的檔案**會把**整份文件**併進 `effective`，兄弟鍵一併進來。
   ⚠️ schema 只在**頂層鍵全部落在白名單內**時才放行（`additionalProperties: false` ＋ 15 個
   固定 properties ＋ `^_state_` / `^_routing` patternProperties），所以「省略 `defaults:`
   直接裸寫閾值鍵」其實會被擋下。repo 內唯一的實例是
   `rule-packs/recipes/examples/conf.d/finance/_defaults.yaml`（頂層只有 `_custom_alerts`）。
   理由與既有防護見
   `blast_radius.py` 的 `PLATFORM_DOCUMENT_KEYS` 註解。⇒ 該形狀對可達性 gate 隱形是
   [#1552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1552) 的主題。
2. **`_custom_alerts` 由 ADR-024 的 UNION 解析器在 unwrap 之後注入**（`describe_tenant.py`，
   #772），**即使有 `defaults:` 包裝也會進 `effective`**；Go 沒有這條注入路徑。實測同一份
   輸入：Python 得 `{cpu_usage, _custom_alerts, _custom_alerts_resolution}`、Go 只得
   `{cpu_usage}` ⇒ **兩實作的 `effective` 不同集，`merged_hash` 因此不等** ⇒ [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)。
3. **`defaults:` 存在但值為 explicit `null`**（schema 的 `type: ["object","null"]` 明文允許，
   `check_confd_schema.py` 實測放行）：Go 的型別斷言 `m["defaults"].(map[string]any)` 失敗 ⇒
   fall-through 併整份文件；Python 的 `ddata.get("defaults", ddata)` 對「鍵存在但值為 `None`」
   回傳 `None`（**不是** fallback）⇒ `deep_merge` 直接 `AttributeError`，`describe_tenant`
   整支 crash。⇒ 尚未開票。

⛔ `tests/golden/fixtures` 的 `_defaults.yaml` 目前全為「有包裝、零兄弟鍵」形狀，**golden
parity 套件結構上偵測不到上述任一例外** —— 不要把它的綠燈讀成「兩實作全等」的背書。

**為什麼不把兄弟鍵併進來**（三條，同等承重）：

1. **會製造 reload 歸因噪音**。`merged_hash` 是 **reload 歸因與 blast-radius 訊號的輸入**：
   `config_debounce.go` 用它決定一次 defaults 變更記成 `applied`
   （`IncReloadTrigger(ReloadReasonDefaults)`）還是 `shadowed` / `cosmetic`，以及
   `da_config_blast_radius_tenants_affected` 報幾個租戶。（rebuild 本身無條件跑
   `fullDirLoad`，**不**以 `merged_hash` 為條件。）而本 ADR 存在的理由正是上面那句「如何避免 reload
   風暴」。把 exporter 根本不消費的 `_routing_defaults` 放進來，會讓每一次平台路由編輯把
   **每一個**租戶都記成受影響 —— 正是被否決的替代方案 A 的後果。
2. **既存快照一次作廢**。`merged_hash` 是 `da-tools tenant-verify --expect-merged-hash` 的
   比對值，擴張定義域會讓所有既存快照失配。
3. **deep_merge 表達不了那個語意**。`_routing_defaults` 是被**不同鍵名**的 `_routing` 以頂層
   淺覆蓋（`_grar_merge.py`），不是同鍵深合併。實測：改一次平台
   `_routing_defaults.group_wait`，5 個租戶全被記為受影響，而其中自帶 `_routing` 的那個租戶
   實際 route 完全不變 —— 該筆歸因可證為假。

⚠️ **這不代表那些鍵是死的。** 它們各自由別的路徑到達租戶：`state_filters` /
`optional_overrides` 由 `pkg/config` 的 merge 併入 `ThresholdConfig` 並在 resolve 期逐租戶
展開；`_routing_defaults` / `_routing_enforced` 由 `generate_alertmanager_routes.py` 的四層
合併消費。（⚠️ `_routing_defaults` 與租戶層 `_routing` **都不進** exporter 的
`ThresholdConfig`：`ResolveRouting()` 至今沒有 production 呼叫端，`types.go` 逐字註明它
「is currently not called by the exporter」，保留為 guardrail 參考實作。上面第 3 條的機制
根源是 `_grar_merge.py` 的 `merge_routing_with_defaults` 本身，與 exporter 讀不讀無關。）

**實測**：它們的套用不依賴 `merged_hash` —— **在有 `defaults:` 包裝的形狀下**，只改
`state_filters.<filter>.severity` 時設定確實生效而 `merged_hash` 逐字不動（⚠️ wrapper-less
形狀下同一筆編輯會讓**每個**租戶的 hash 都動，見上方例外 1）。兩條載入路徑都不以
**`merged_hash`** 為條件：flat 模式下任何 `_` 前綴檔變更都走 full rebuild（`config.go` 的
`isTenantOnlyChange`），hierarchical 模式下 `installNewHierarchyState` 每次都跑 `fullDirLoad`。
⚠️ flat 路徑另有一道 per-file composite hash 的 no-op 快篩會提早返回（`config.go` 的
`compositeHash == prevHash`）—— 那是**不同的 hash**，不是 `merged_hash`。

⚠️ **代價是已知且刻意的**：「平台面變更」對所有以 `effective_config` / `merged_hash` 為輸入
的消費端**結構上不可見** —— `GET /effective`、`describe_tenant`、`blast_radius`、what-if 預覽
（`handler_simulate.go`、Portal `simulate-preview.jsx`）、`da-guard`。

⛔ 其中 **`da-tools tenant-verify --expect-merged-hash` 不是診斷而是閘門**（rollback
checklist 的擋下訊號，exit 2 = 不一致）：平台面 rollback 之後它會回 exit 0，而**那代表
「這一面沒被涵蓋」，不代表「rollback 已驗證」**。

那是 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) 的主題，
處置方式是另建一個平台面比較平面，**不是**擴張這裡的定義域。

### Dual-Hash 機制

每個 tenant 維護兩個 hash：

| Hash | 定義 | 用途 |
|:-----|:-----|:-----|
| `source_hash` | SHA-256 of tenant YAML file bytes，**截斷為前 16 個 hex 字元** | 判斷 tenant 原始檔案是否變動 |
| `merged_hash` | SHA-256 of effective config (merge 後的 canonical JSON)，**截斷為前 16 個 hex 字元** | 判斷最終生效設定是否變動 |

⛔ **兩者都是 16 字元，不是完整的 64 字元 digest** —— 直接 `sha256sum` 算出來的值永遠不會 match
`--expect-merged-hash`。另注意**同名不同物**：scanner 內部的 `m.hierarchy.hashes` 是 per-file 的
**64 字元** SHA-256，與表中的 `source_hash` 撞名，但不是同一個值。

**Reload 判斷邏輯**：

```
if source_hash changed:
    recompute effective config → update merged_hash
    記為 applied（reason=source；此租戶初次出現則 reason=new）
    ⚠️ 這條分支不比對 merged_hash —— 見下方註記
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        記為 applied（reason=defaults）
    else:
        記為 shadowed / cosmetic（見 §Amendment 2026-04-25）
```

⚠️ **這段虛擬碼描述的是「一次變更被歸類成什麼」，不是「rebuild 會不會發生」。**
實作中 hierarchical 路徑的 `diffAndReload` 在 `classifyAndCount` 之後**無條件**呼叫
`installNewHierarchyState`，而後者第一件事就是無條件跑 `fullDirLoad`。`merged_hash` 決定的是
這次變更記成 `applied`（`IncReloadTrigger`）還是 `shadowed` / `cosmetic`，**不決定重建**。

⛔ **本 ADR 原文在 `source_hash` 分支也寫了一次 `merged_hash` 比對，那在實作裡不存在。**
`config_debounce.go` 的 `if sourceChanged` 直接記為 `applied`，`prev == mh` 的比對只出現在
`else if defaultsChanged`。上方虛擬碼已改寫為實際行為。⚠️ 後果是：**純註解編輯一個租戶
YAML 也會被記成 `applied`**，污染 blast-radius 的高影響訊號——這是已知落差，不是本 ADR 的決策。

### 繼承圖資料結構

Scanner 維護一個 **inheritance graph**：

```go
type InheritanceGraph struct {
    // _defaults.yaml 路徑 → 受影響的 tenant ID 清單
    DefaultsToTenants map[string][]string
    // tenant ID → 其繼承鏈上的 _defaults.yaml 路徑（ordered, L0→L3）
    TenantDefaults    map[string][]string
}
```

⚠️ **`DefaultsToTenants` / `TenantsAffectedBy` 目前沒有 production 消費端**（全 repo 命中只在
`inheritance_graph.go` 自身的建構子與存取器，加上測試）。實際的 reload 路徑 `classifyAndCount`
對所有掃到的租戶迭代，取的是**反方向**的 `TenantDefaults[tid]`，再靠 per-tenant 的 `merged_hash`
比對跳過 recompute ——「避免全量重算」是那個比對達成的，不是這張反向表。這張表保留為既有結構，
改動它不會改變行為。

### Watch 機制：維持 Periodic Scan

- **不採用 inotify/fsnotify**：container mount 事件遺失 + kernel watch 上限
- 維持既有 periodic scan（可設定 interval，default 30s）
- ⚠️ **「只重算 `stat()` 變動的檔案」尚未實作**：`scanDirHierarchical` 的 `priorMtimes` 參數
  目前被忽略（`config_hierarchy.go` 逐字 `_ = priorMtimes // reserved for Phase 3`），每次掃描
  對走訪到的**每個**檔案無條件 `sha256.Sum256`。benchmark 數字即為全量 hash 的成本。

### Debounce

- `git pull` 落地 50 檔案時，每個 `stat()` 變動不立即觸發 reload
- Debounce window: **300ms**（可設定，`--scan-debounce` flag）
- Window 內累積所有變動 → 一次性 batch recompute → 一次性 reload
- 避免 reload 風暴（50 個 tenant 各 reload 一次 → 變成只 reload 一次）

### Cardinality Guard

- `_defaults.yaml` **本身不產生 Prometheus metric series**
- 繼承欄位仍遵循既有 Cardinality Guard 規則（v2.5.0 ADR-005）
- `merged_hash` label 不暴露在 metrics（防 label 爆炸）

### 新增 Prometheus Metrics

| Metric | Type | Labels | Description |
|:-------|:-----|:-------|:------------|
| `da_config_scan_duration_seconds` | histogram | — | 單次 periodic scan 耗時 |
| `da_config_reload_trigger_total` | counter | `reason` | reload 原因，**五個值**：source / defaults / new / delete / **forced**（手動觸發）。⚠️ 與下方 `blast_radius` 的 `reason` **定義域不同** —— 那個只有前四個，forced 被過濾 |
| `da_config_defaults_change_noop_total` | counter | — | defaults 變動但 merged_hash 不變的**歸類**次數。⚠️ **不是「被省下的 rebuild」** —— rebuild 無條件執行（見 §Reload 判斷邏輯下方註記） — **v2.8.0 起語義收窄為 cosmetic-only**（見 §Amendment 2026-04-25） |
| `da_config_defaults_shadowed_total` | counter | — | **v2.8.0 (Issue #61)** — defaults 變動但被 tenant override 擋下的次數（從 `da_config_defaults_change_noop_total` 拆出） |
| `da_config_blast_radius_tenants_affected` | histogram | `reason / scope / effect` | **v2.8.0 (Issue #61)** — 每 tick 受影響 tenant 數的分佈 |

### Amendment 2026-04-25 (Issue #61): noop 語義拆分

原 §Reload 判斷邏輯把「comment-only edit」與「override-shadowed edit」都記為 `da_config_defaults_change_noop_total`，使 ops 無法區分「真的沒事」vs「繼承機制擋下變動」。v2.8.0 後拆為兩個 effect：

```
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        trigger reload
        emit blast_radius{effect="applied"}
    else:
        # 進一步拆分（Issue #61）
        compute changedKeys = diff(prior_parsed_defaults, new_parsed_defaults)
        if len(changedKeys) == 0:
            # 純 cosmetic：comment-only / reordering / whitespace
            increment da_config_defaults_change_noop_total
            emit blast_radius{effect="cosmetic"}
        elif tenantOverridesAll(tenant_src, changedKeys):
            # Shadowed：tenant 覆寫了所有變動的 key
            increment da_config_defaults_shadowed_total
            emit blast_radius{effect="shadowed"}
        else:
            # 邏輯上不可達（merged_hash 應已移動）
            # — 防禦性 fallback 至 cosmetic
            increment da_config_defaults_change_noop_total
```

實作要點：
- `m.hierarchy.parsedDefaults` 與 `m.hierarchy.hashes`（v2.8.0 起收進 `hierarchyState`
  sub-struct；本 ADR 原文寫的 `m.parsedDefaults` / `hierarchyHashes` 已不是實際欄位名）同
  atomic-swap，存放每個 `_defaults.yaml` 的 normalized parsed dict（`map[string]any`），記憶體 ~1MB / 1000 tenants
- 在 `populateHierarchyState` cold-start 時 eager-parse 全部 defaults；`diffAndReload` 時只重新 parse 有 hash 變動的檔案，未變動的沿用前值
- 詳見 `components/threshold-exporter/app/config_defaults_diff.go` + Issue #61 RFC

## 考量的替代方案

### A: Single-Hash（僅 source_hash）

❌ `_defaults.yaml` 變動時無法判斷哪些 tenant 真正受影響，
只能全量 reload。1000+ tenant 環境下 reload 風暴不可接受。

### B: fsnotify / inotify

❌ 在 container mount（NFS/FUSE/projected volume）環境下事件遺失是已知問題。
kernel watch 限制（default 8192）在千租戶環境會被用盡。
v2.5.0 已驗證 periodic scan 在 2000 tenant 下 < 200ms（v2.7.0 規劃期 baseline 確認）。

### C: Array Concat（而非 Replace）

❌ `group_by: [severity]`（L0）+ `group_by: [alertname]`（L1）
→ concat 結果 `[severity, alertname]` 語意不明確。
用戶預期「我覆蓋了 group_by」而非「我追加了」。
Replace 語意更直覺，且與 Helm values merge 行為一致。

## 影響

- **Directory Scanner Go 程式碼**：新增 inheritance graph + dual-hash + debounce
- **CLI**：新增 `describe-tenant` 可展開 effective config + 顯示繼承來源
- **Tenant API**：新增 `GET /api/v1/tenants/{id}/effective` endpoint
- **Schema**：新增 `platform-defaults.schema.json` 供 `_defaults*.yaml` 使用。⚠️ **不是**升級
  `tenant-config.schema.json` —— 後者 root 只有 `tenants` 且 `additionalProperties: false`，
  結構上表達不了平台預設；分流見 `check_confd_schema.py`
- **Benchmark**：千租戶 + 多層繼承的 scan 效能對照 v2.7.0 規劃期 baseline（已驗證）

## 相關

- [ADR-016: conf.d/ 目錄分層 + 混合模式](016-conf-d-directory-hierarchy-mixed-mode.md)
- [Benchmark Report §1 規模](../benchmarks.md#1-規模能撐多少租戶) — dual-hash 1000-tenant 實測 + SLO 判讀
- [architecture-and-design.md §設計概念](../architecture-and-design.md#設計概念總覽)
