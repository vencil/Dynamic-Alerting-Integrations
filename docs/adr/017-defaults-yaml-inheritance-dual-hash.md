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
    之後覆寫該鍵）。⛔ **這是 Python-only**：Go 側沒有這條路徑、仍走 REPLACE，兩實作的
    `effective` 因此不同集（見 §已知的可達例外 2 與 #1549）。
- **Scalar 欄位**：子層覆蓋父層
- **Null 值 — 依欄位而分，不是通則**（#1339 拆開原本合寫的一行）：
  - **路由的四個欄位**（`_routing` 底下的 `group_by` / `group_wait` /
    `group_interval` / `repeat_interval`）：顯式 `null` **退出繼承**，產出的 route
    省略該欄位。這些欄位沒有 `"disable"` 哨兵值，只能用「拿掉值」來表達。
    ⚠️ 但 `null` **不是唯一的寫法**：四個欄位全都是 falsy 檢查（`_grar_merge.py` 的
    `if val:` 管三個時間欄位、`_grar_routes.py` 的 `if group_by and isinstance(group_by,
    list)` 管 `group_by`），所以 `""` / `0` / `[]` 同樣會讓欄位被省略。（另注意 `group_by`
    並不是時間欄位。）
    `_routing.receiver` 與 `_routing.overrides` **不適用**：前者會讓該租戶整條
    route 消失（告警落到 catch-all），後者無上層可退。
  - **閾值 key**：顯式 `null` **不退出繼承**，請改用 `"disable"`。發射面
    （`collector.go` → `ResolveAtWithStats`）本來就會忽略 null 並回退平台預設；
    診斷面（`/effective`、`describe_tenant`、simulate）已於 #1339 對齊。
  - **其餘 `_` 前綴的保留 key**：顯式 `null` **退出繼承**——`pkg/config/hierarchy.go` 的
    `deepMerge` 對任何 `_` 前綴鍵的 explicit null 做 `delete(result, k)`，非 `_` 前綴
    （＝閾值鍵）只 `continue`。該處註解把本 ADR 指為這條規則的權威，所以規則寫在這裡：
    **判準是「是否 `_` 前綴」，不是「是否路由欄位」。**
    ⛔ **但這條只在 `_defaults.yaml` 側可用。** `tenant-config.schema.json` 對
    `_silent_mode` / `_profile` / `_severity_dedup` / `_namespaces` / `_custom_alerts` 都宣告
    了非 null 型別，**租戶檔**寫 `null` 一律被 `check_confd_schema.py` 擋下；
    `platform-defaults.schema.json` 對這些鍵是寬鬆 sub-schema（`defaults` 的內部逐字宣告
    「values left loose」），所以 `_defaults.yaml` 的頂層或 `defaults:` 內部寫 `null` 才會
    放行。實際可達的路徑是 defaults 檔 → defaults 檔。
    ⛔ **而且被繼承的值與那個 `null` 必須在同一個位置**：`_` 前綴鍵若寫在 `defaults:` 的
    **兄弟**位置，在有包裝的形狀下根本不會進 `effective`（見 §給要編輯的人 第 1 條），也就
    沒有東西可刪 —— 寫了是靜默 no-op。真正會生效的組合是「兩者都在 `defaults:` 內部」或
    「兩者都在無包裝檔的頂層」。
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
effective = deep_merge( defaults_block(L0), …, defaults_block(Ln), tenant_body )

  其中 defaults_block(f) = f["defaults"]
       ⤷ 該鍵缺席、或存在但值為 null 時，退回 f 本身（＝整份文件；見下方例外 1 / 3）
```

兩個 unwrap 實作：`describe_tenant.py` 的 `ddata.get("defaults", ddata)`、Go 的
`pkg/config.extractDefaultsBlock`（`config_inheritance.go` 另有一份同名副本）。

### 給要編輯 `_defaults.yaml` 的人：這四條

1. **只有 `defaults:` 區塊裡的鍵會進 `effective`。** 與它**平級**的頂層鍵（`state_filters` /
   `optional_overrides` / `profiles` / `max_metrics_per_tenant` / `_routing*` 等）**照樣生效**，
   只是各走各的管線：`state_filters` / `optional_overrides` 由 `pkg/config` 的 merge 併入
   `ThresholdConfig` 並在 resolve 期逐租戶展開，`_routing_defaults` / `_routing_enforced` 由
   `generate_alertmanager_routes.py`（實作在它匯入的 `_grar_*` 模組）的四層合併消費。它們不經過
   `effective` / `merged_hash`。
   ⛔ **哪些鍵允許出現，見 [`platform-defaults.schema.json`](../schemas/platform-defaults.schema.json)
   ——但那是「這個檔案允許哪些頂層鍵」的清單，不是「這個鍵放在這裡就會生效」的清單。**
   `_silent_mode` / `_profile` / `_severity_dedup` 都列在該 schema 的頂層，但它們真正被消費的
   位置是 `_defaults.yaml` 裡的 **`tenants:` 區塊**；寫在與 `defaults:` 平級的頂層是**靜默
   no-op**（實測：exporter 零 WARN、`merged_hash` 不變、schema lint 回 `OK`）。
   ⛔ 允許出現的鍵以那份 schema 為準，**勿在本 ADR 另立一份列舉**——上面括號裡的幾個名字是
   舉例，不是清單。

2. ⛔ **不要把平級鍵縮排進 `defaults:` 想讓它們「被看見」。三個平面下場不同，其中一個會讓客戶
   收不到告警**：
   - **exporter**：欄位型別是 `map[string]float64`（`types.go`），巢狀 mapping 會 unmarshal
     失敗 ⇒ `parsePartialConfig` 回 `ok=false`、**整份檔案被丟棄**並 log
     `ERROR: ... entire block dropped`。連同檔內其他平級鍵一起陪葬。
   - **路由**：⛔ `_routing_defaults` 一旦離開頂層，`generate_alertmanager_routes.py` 就找不到
     它——**沒有自己 `_routing` 的租戶會整條 route ＋ receiver 消失**（實測：`Found 2 tenant(s)
     with routing config: db-a, db-b` → `Found 1 tenant(s): db-b`，`tenant-db-a` receiver 消失，
     **RC=0、零 error、零 warning**）。`check_confd_schema.py` 也**不擋**（實測兩者皆 `RC=0`；
     `defaults` 的 sub-schema 逐字宣告 values left loose）。
   - **`effective`**：**靜默接受**成一個巢狀鍵，而 blast-radius 會因此從「無變更」變成一份
     Tier B 報告。⚠️ **也就是說：診斷面會正向獎勵這個動作，而它同時讓一個租戶失去告警。**

3. **改了平級鍵之後，不要拿 `merged_hash` / `/effective` / `blast_radius` 去確認它生效**
   （那三個面看不到，而且執行期會把它標成 `effect="cosmetic"`，見下方「診斷面的代價」）。
   **改去問真正的消費端**：
   - `state_filters` → exporter `/metrics` 的 `user_state_filter{tenant,filter,severity}`
   - `_silent_mode` → `user_silent_mode{tenant,target_severity}`
   - `_routing_defaults` / `_routing_enforced` → `generate_alertmanager_routes.py --config-dir
     conf.d/ --dry-run`，比對前後的 `Found N tenant(s) with routing config` 與 receiver 集合

4. **想用顯式 `null` 退掉一個繼承來的 `_` 前綴鍵：`null` 必須與被繼承的那個值在同一個位置。**
   判準是「是否 `_` 前綴」（`pkg/config/hierarchy.go` 的 `deepMerge` 對 `_` 前綴鍵做
   `delete(result, k)`，非 `_` 前綴只 `continue`）。可用的組合只有兩種：**兩者都在 `defaults:`
   內部**，或**兩者都在無 `defaults:` 包裝的檔案頂層**。⛔ 寫在 `defaults:` 的**兄弟**位置＝
   沒有東西可刪＝靜默 no-op；⛔ 寫在**租戶檔**一律被 `check_confd_schema.py` 擋下
   （`tenant-config.schema.json` 對這些鍵宣告了非 null 型別）。

### 已知的可達例外（非窮舉——這份清單不是保證）

1. **無 `defaults:` 鍵的檔案**會把**整份文件**併進 `effective`，兄弟鍵一併進來。
   ⚠️ schema 只在**頂層鍵全部落在白名單內**時才放行（`additionalProperties: false` ＋ 15 個
   固定 properties ＋ `^_state_` / `^_routing` patternProperties），所以「省略 `defaults:`
   直接裸寫閾值鍵」其實會被 `check_confd_schema.py` 擋下。repo 內有**兩個**檔案沒有 `defaults:`
   鍵，但只有 `rule-packs/recipes/examples/conf.d/finance/_defaults.yaml` 真的帶內容
   （頂層只有 `_custom_alerts`）；另一個 `rule-packs/recipes/examples/conf.d/_defaults.yaml`
   全檔只有註解、解析成 `None`，併不進任何東西。
   ⇒ 該形狀對可達性 gate 隱形是
   [#1552](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1552) 的主題。

2. **`_custom_alerts` 由 ADR-024 的 UNION 解析器在 unwrap 之後注入**（`describe_tenant.py`，
   #772），**即使有 `defaults:` 包裝也會進 `effective`**；Go 沒有這條注入路徑。實測同一份輸入：
   Python 得 `{cpu_usage, _custom_alerts, _custom_alerts_resolution}`、Go 只得 `{cpu_usage}`
   ⇒ **兩實作的 `effective` 不同集，`merged_hash` 因此不等** ⇒
   [#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)。

3. **`defaults:` 存在但值為 explicit `null`**（schema 的 `type: ["object","null"]` 明文允許，
   `check_confd_schema.py` 實測放行）：Go 的型別斷言 `m["defaults"].(map[string]any)` 失敗 ⇒
   fall-through 併整份文件；Python 的 `ddata.get("defaults", ddata)` 對「鍵存在但值為 `None`」
   回傳 `None`（**不是** fallback）⇒ `deep_merge` 直接 `AttributeError`，`describe_tenant`
   整支 crash。⇒ 尚未開票。

⛔ `tests/golden/fixtures` 的 `_defaults.yaml` 目前全為「有包裝、零兄弟鍵」形狀，**golden
parity 套件結構上偵測不到上述任一例外** —— 不要把它的綠燈讀成「兩實作全等」的背書。

### 診斷面的代價（刻意接受）

平台面變更對所有以 `effective_config` / `merged_hash` 為輸入的消費端**結構上不可見**：
`GET /effective`、`describe_tenant`、`blast_radius`、what-if 預覽（`handler_simulate.go`、
Portal `simulate-preview.jsx`）、`da-guard`。以下兩格比「看不到」更危險：

⛔ **執行期會貼錯標籤，不只是漏看。** `config_defaults_diff.go` 的 `parseDefaultsBytes` 走同一個
unwrap，所以 `classifyDefaultsNoOpEffect` 拿不到兄弟鍵——一次「改平台 severity ＋ 改路由」的
真實變更，與「加了一行註解」得到**逐字相同**的 `effect="cosmetic"`。SRE 看到
`blast_radius{effect="cosmetic"}` 會讀成「只是改註解」。

⛔ **`da-tools tenant-verify --expect-merged-hash` 是閘門不是診斷**（rollback checklist 的擋下
訊號，exit 2 = 不一致）：平台面 rollback 之後它會回 exit 0，而**那代表「這一面沒被涵蓋」，不代表
「rollback 已驗證」**。

⇒ 這是 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) 的主題，
處置是**另建一個平台面比較平面**。為什麼不直接擴張這裡的定義域，見本文件的
**§考量的替代方案 D**。

### Dual-Hash 機制

每個 tenant 維護兩個 hash：

| Hash | 定義 | 用途 |
|:-----|:-----|:-----|
| `source_hash` | SHA-256 of tenant YAML file bytes，**截斷為前 16 個 hex 字元** | 判斷 tenant 原始檔案是否變動 |
| `merged_hash` | SHA-256 of effective config (merge 後的 canonical JSON)，**截斷為前 16 個 hex 字元** | 判斷最終生效設定是否變動 |

⛔ **兩者都是 16 字元，不是完整的 64 字元 digest。** scanner 內部的 `m.hierarchy.hashes` 存的
是**未截斷的 64 字元** SHA-256——與 `source_hash` 是**同一個 digest**，租戶檔那筆的前 16 字元
就是表中的 `source_hash`。⛔ 但 `merged_hash` 雜湊的是 canonical JSON、**不是**檔案 bytes，
所以對任何檔案跑 `sha256sum` 都不會 match `--expect-merged-hash`。

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

⛔ **`source_hash` 那條分支不比對 `merged_hash`**：`config_debounce.go` 的 `if sourceChanged`
直接記為 `applied`，`prev == mh` 的比對只出現在 `else if defaultsChanged`。⚠️ 後果是：**純註解
編輯一個租戶 YAML 也會被記成 `applied`**，污染 blast-radius 的高影響訊號。這是實作現況與本 ADR
意圖之間的已知落差，不是刻意的設計。

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

⚠️ **`DefaultsToTenants` / `TenantsAffectedBy` 目前沒有 production 消費端**（讀取端只有
`inheritance_graph.go` 自身的存取器與測試）。實際的 reload 路徑 `classifyAndCount` 對所有掃到
的租戶迭代，取的是**反方向**的 `TenantDefaults[tid]`，並以**每個檔案的 SHA-256 比對**
（`scan.hashes` vs `prior.hashes`，涵蓋租戶檔與其整條 defaults 鏈）判定該租戶是否需要重算；
不需要時直接沿用上一輪快取的 `merged_hash`。⛔ `merged_hash` 自己的比對（`prev == mh`）發生在
recompute **之後**，右運算元就是 recompute 的產物，因此**省不下任何 recompute**——它只決定歸類
成 `applied` 還是 `shadowed` / `cosmetic`。「避免全量重算」是那個**檔案雜湊**比對達成的，不是
這張反向表。這張表保留為既有結構，改動它不會改變行為。

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
| `da_config_reload_trigger_total` | counter | `reason` | reload 原因。**實際發射的只有四個值**：source / defaults / new / delete（全部來自 `classifyAndCount`，僅 hierarchical 模式）。⚠️ `config_metrics.go` 的宣告把 `forced` 也列進定義域，但**沒有任何 production 路徑用它當 label**：`ReloadReasonForced` 由 `detectChange()` 在 hierarchical 模式回傳（**不是**常數註解說的手動 / SIGHUP 觸發），只流進 debounce 的 `pendingReasons`（僅取長度餵 `da_config_debounce_batch`）。下方 `blast_radius` 的 `reason` 實際定義域與此**相同** |
| `da_config_defaults_change_noop_total` | counter | — | defaults 變動但 merged_hash 不變的**歸類**次數。⚠️ **不是「被省下的 rebuild」** —— rebuild 無條件執行（見 §Reload 判斷邏輯下方註記） — **v2.8.0 起語義收窄為 cosmetic-only**（見 §Amendment 2026-04-25） |
| `da_config_defaults_shadowed_total` | counter | — | **v2.8.0 (Issue #61)** — defaults 變動但被 tenant override 擋下的次數（從 `da_config_defaults_change_noop_total` 拆出） |
| `da_config_blast_radius_tenants_affected` | histogram | `reason / scope / effect` | **v2.8.0 (Issue #61)** — 每 tick 受影響 tenant 數的分佈 |

### Amendment 2026-04-25 (Issue #61): noop 語義拆分

原 §Reload 判斷邏輯把「comment-only edit」與「override-shadowed edit」都記為 `da_config_defaults_change_noop_total`，使 ops 無法區分「真的沒事」vs「繼承機制擋下變動」。v2.8.0 後拆為兩個 effect：

```
elif any ancestor _defaults.yaml changed:
    recompute effective config → update merged_hash
    if merged_hash changed:
        記為 applied（IncReloadTrigger(reason=defaults)）
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
  sub-struct）同 atomic-swap，存放每個 `_defaults.yaml` 的 normalized parsed dict（`map[string]any`），記憶體 ~1MB / 1000 tenants
- 在 `populateHierarchyState` cold-start 時 eager-parse 全部 defaults；`diffAndReload` 時只重新 parse 有 hash 變動的檔案，未變動的沿用前值
- 詳見 `components/threshold-exporter/app/config_defaults_diff.go` + Issue #61 RFC

## 考量的替代方案

### A: Single-Hash（僅 source_hash）

❌ `_defaults.yaml` 變動時無法判斷哪些 tenant 真正受影響，
只能全量 reload。1000+ tenant 環境下 reload 風暴不可接受。

### B: fsnotify / inotify

❌ 在 container mount（NFS/FUSE/projected volume）環境下事件遺失是已知問題。
kernel watch 限制（default 8192）在千租戶環境會被用盡。
periodic scan 的實測成本見 [`benchmarks.md`](../benchmarks.md) §1：
**1000 租戶**冷啟動全量載入 **112 ms**、穩態 reload **1.3 ms**。⚠️ 本 ADR 早期版本寫的
「2000 tenant < 200ms」在 repo 內找不到出處，已改為實際可查的數字。

### C: Array Concat（而非 Replace）

❌ `group_by: [severity]`（L0）+ `group_by: [alertname]`（L1）
→ concat 結果 `[severity, alertname]` 語意不明確。
用戶預期「我覆蓋了 group_by」而非「我追加了」。
Replace 語意更直覺，且與 Helm values merge 行為一致。

### D: 擴張 `effective` 的定義域（把兄弟鍵合併進來）— 已否決

❌ 這是 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516) 提出的
直覺解法：既然平台面變更在診斷面不可見，就把 `state_filters` / `_routing_defaults` 等兄弟鍵
也併進 `effective`。**三條理由同等承重，都是量出來的**：

1. **會製造 reload 歸因噪音。** `merged_hash` 是 reload **歸因**與 blast-radius 訊號的輸入：
   `config_debounce.go` 的 `classifyTenant` 用它決定一次 defaults 變更記成
   `applied`（`IncReloadTrigger`）還是
   `shadowed` / `cosmetic`。合併兄弟鍵之後，每一次平台路由編輯都會把每個租戶從 `cosmetic`
   翻成 `applied`，並讓 `da_config_reload_trigger_total{reason="defaults"}` 逐次增量——而本 ADR
   存在的理由正是上面那句「如何避免 reload 風暴」。**這正是上面替代方案 A 被否決的同一個
   後果。**⚠️ 精確地說：租戶**現在就已經**每次都被送進
   `da_config_blast_radius_tenants_affected` 的直方圖，改變的是 `effect` label 與 counter 是否
   增量，不是「有沒有被記」。

2. **既存快照一次作廢。** `merged_hash` 是 `da-tools tenant-verify --expect-merged-hash` 的
   比對值，擴張定義域會讓所有既存快照失配。

3. **deep_merge 表達不了那個語意。** `_routing_defaults` 是被**不同鍵名**的 `_routing` 以頂層
   淺覆寫的（`_grar_merge.py` 的 `merge_routing_with_defaults`），不是同鍵深合併。實測：改一次
   平台 `_routing_defaults.group_wait`，5 個租戶全被記為受影響，而其中自帶 `_routing` 的那個
   租戶實際 route **完全不變** —— 該筆歸因可證為假。

**兩個帶對照組的量測支撐這個決定**：

- 兄弟鍵的**套用**不依賴 `merged_hash`。在有 `defaults:` 包裝的形狀下，只改
  `state_filters.<filter>.severity` 時設定確實生效而 `merged_hash` 逐字不動；對照組（改
  `defaults:` 底下租戶未覆寫的鍵）hash 會動。⚠️ wrapper-less 形狀下同一筆編輯會讓**每個**租戶的
  hash 都動（見 §已知的可達例外 1）。
- 兩條載入路徑都不以 `merged_hash` 為條件：flat 模式下任何 `_` 前綴檔變更都走 full rebuild
  （`config.go` 的 `isTenantOnlyChange`），hierarchical 模式下 `installNewHierarchyState` 每次
  都跑 `fullDirLoad`。⚠️ flat 路徑另有一道 composite hash（全目錄一顆）的 no-op 快篩會提早返回
  （`config.go` 的 `compositeHash == prevHash`）—— 那是**不同的 hash**。

⚠️ **`_routing` 系列對 exporter 的狀態不對稱，值得記一筆**：`_routing_defaults` 沒有對應的
`ThresholdConfig` 欄位、解碼即丟；租戶層 `_routing` **會**被載進 `ThresholdConfig.Tenants`
（`resolveBaseRows` 必須明文跳過 `_routing*` 前綴正因為它在那個 map 裡），但**沒有任何
production 呼叫端消費它**——`ResolveRouting()` 只被測試呼叫，`types.go` 逐字註明它
「is currently not called by the exporter」，保留為 guardrail 參考實作。它在 repo 內另有兩個
真實消費端：`generate_alertmanager_routes.py` 的四層合併，以及 `cmd/da-guard`（讀
`EffectiveConfig["_routing"]` 建 `RoutingByTenant`）。

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
