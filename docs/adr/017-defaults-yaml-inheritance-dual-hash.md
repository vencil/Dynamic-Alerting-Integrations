---
title: "ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載"
tags: [adr, defaults, inheritance, hot-reload, dual-hash, v2.7.0]
audience: [platform-engineers, sre, contributors]
version: v2.9.0
lang: zh
id: ADR-017
tracking_kind: adr
status: accepted
domain: exporter
created_at: 2026-04-18
updated_at: 2026-10-09
---
# ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載

> **Language / 語言：** **中文 (Current)** | [English](./017-defaults-yaml-inheritance-dual-hash.en.md)

> 與 [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)（conf.d/ 目錄分層）為一組：ADR-016 決定設定檔怎麼分目錄放，本篇決定各層的預設值怎麼往下傳，以及預設值改了之後怎麼判斷影響了哪些租戶。

**決策摘要**：conf.d/ 的每一層目錄都可以放一份 `_defaults.yaml`，租戶的設定是從根目錄往下逐層疊上預設值、最後疊上租戶檔的結果。每個租戶記兩個雜湊：租戶檔本身的，與疊完之後的最終設定的；後者用來判斷一次預設值變更有沒有真的改到這個租戶。

## 狀態

✅ **Accepted**（v2.7.0，2026-04-19）。之後的修訂都已併入下文對應段落：

| 日期 | 修訂內容 | 在本文的位置 |
|:--|:--|:--|
| 2026-04-25 | 「預設值改了、租戶的最終設定沒變」拆成兩種：被租戶覆蓋（shadowed）與沒有實質變更（cosmetic） | 決策 7 |
| 2026-09-28 | 路由設定也沿目錄逐層繼承 | 決策 9 |
| 2026-10-08 | 最終設定逐字顯示寫下的值，`/metrics` 不送的值逐鍵標出 | 決策 10 |

## 名詞

- **租戶檔**：檔名不以 `_` 開頭、用 `tenants:` 宣告租戶的檔。**平台檔**：檔名以 `_` 開頭的檔，例如 `_defaults.yaml`。
- **最終設定（effective config）**：各層預設值依序疊上、再疊上租戶檔之後，這個租戶適用的設定。用 `da-guard effective`、tenant-api 的 `GET /api/v1/tenants/{id}/effective` 或 `describe_tenant.py` 查看。
- **da-guard**：本 repo 的 conf.d/ 檢查工具。`da-guard effective` 印出 tenant-api `/effective` 會給的最終設定，`da-guard served-values` 印出 exporter `/metrics` 會送的值。
- **`/metrics`**：exporter 給 Prometheus 抓的指標，閾值以 `user_threshold` series 送出。`da-guard served-values` 印出它會送的值。
- **熱重載**：exporter 不重啟，定期重新掃描目錄並套用新設定。

## 背景

原本 conf.d/ 只有根目錄一份 `_defaults.yaml`。ADR-016 讓 conf.d/ 能分目錄之後，要回答三個問題：

1. 哪些目錄可以放 `_defaults.yaml`？
2. 上下層的值怎麼合併？
3. 某一層的 `_defaults.yaml` 改了，哪些租戶受影響？

既有的熱重載只對租戶檔算雜湊（SHA-256），判斷「這個檔有沒有變」。但租戶的最終設定也取決於它繼承的預設值，光看租戶檔回答不了第 3 題，所以需要第二個雜湊。

## 決策

### 1. 每一層目錄都可以放 `_defaults.yaml`

```
conf.d/
├── _defaults.yaml              ← 根目錄：全平台預設
├── {domain}/
│   ├── _defaults.yaml          ← domain 層
│   └── {region}/
│       ├── _defaults.yaml      ← region 層
│       └── {env}/
│           ├── _defaults.yaml  ← env 層
│           └── tenant-001.yaml
```

套用順序是根目錄 → 往下每一層 → 租戶檔，後套用的覆蓋先套用的。每一層都可以省略，層數也不限於上圖。

### 2. 合併規則

| 值的型別 | 規則 | 例子 |
|:--|:--|:--|
| 對照表（mapping） | 深合併：子層新增的鍵保留，同名鍵由子層覆蓋 | 上層 `{p: 1, q: 1}`、租戶 `{q: 9}` → `{p: 1, q: 9}` |
| 清單（list） | 整個取代，不串接 | 上層 `[ns-a, ns-b]`、租戶 `[ns-c]` → `[ns-c]` |
| 純量（數字、字串） | 子層覆蓋 | 上層 `200`、租戶 `"150"` → `"150"` |

兩個例外：

- **`_metadata` 不繼承**：上層寫的 `_metadata` 不會出現在租戶的最終設定裡。
- **`_custom_alerts`（租戶自訂告警，見 [ADR-024](024-version-aware-threshold-via-dimensional-label.md)）在兩個實作裡不同**：`describe_tenant.py` 算的最終設定是聯集，上層 `_defaults.yaml` 頂層宣告的清單加上租戶自己的清單；tenant-api 與 da-guard 算的只有租戶自己的清單。兩邊的 `merged_hash` 因此不同（[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)）。

### 3. 範例

`conf.d/_defaults.yaml`（根目錄）：

```yaml
defaults:
  pg_stat_activity_count: 500
  pg_replication_lag_seconds: 30
  pg_locks_count: 300
```

`conf.d/finance/_defaults.yaml`（domain 層，金融更嚴格）：

```yaml
defaults:
  pg_stat_activity_count: 200
  pg_locks_count: 100
```

`conf.d/finance/fin-db-001.yaml`（租戶檔）：

```yaml
tenants:
  fin-db-001:
    pg_stat_activity_count: "150"
```

`da-guard effective --config-dir conf.d` 的輸出（節錄）：

```json
"effective_config": {
  "pg_locks_count": 100,
  "pg_replication_lag_seconds": 30,
  "pg_stat_activity_count": "150"
},
"key_sources": {
  "pg_locks_count":             {"layer": "defaults", "file": "finance/_defaults.yaml", "level": 1},
  "pg_replication_lag_seconds": {"layer": "defaults", "file": "_defaults.yaml", "level": 0},
  "pg_stat_activity_count":     {"layer": "tenant",   "file": "finance/fin-db-001.yaml"}
},
"source_hash": "45006e7b8bf54ba3",
"merged_hash": "5db367c3efd997ce"
```

`da-guard served-values --config-dir conf.d` 顯示 `/metrics` 送出同樣三個值：`pg_locks_count` 100、`pg_replication_lag_seconds` 30、`pg_stat_activity_count` 150。

讀這個範例要注意兩件事：

- **租戶的值加引號，`defaults:` 裡不加。** 租戶的閾值是字串或帶排程的物件，未加引號的數字會被 `check_confd_schema.py` 擋下；平台預設只收數字。
- **子目錄只能改根目錄已宣告的閾值。** 把根目錄的 `pg_locks_count` 拿掉，最終設定仍顯示 100，但 `/metrics` 不送它。`da-guard effective` 的每個租戶有一個 `not_served` 欄位，逐鍵列出「最終設定顯示了、`/metrics` 卻不送」的值與原因，這裡標 `undeliverable`；da-guard 的檢查報 `subtree_default_undeliverable`。

### 4. 閾值寫 `null` 等於這一層沒寫

閾值鍵寫 `null` 不會關掉告警；要關掉請寫 `"disable"`。

- 租戶檔或子目錄 `_defaults.yaml` 寫 `null` 時，`/metrics`（`da-guard served-values`）、`/effective`（`da-guard effective`）與 `describe_tenant` 的結果與這一層沒寫這個鍵相同。
- 根目錄 `_defaults.yaml` 的 `defaults:` 寫 `null` 時，根層沒有宣告這個閾值，`/metrics` 不送出這個閾值的 series（不是門檻 0）：租戶給的值由 da-guard 的 `root_default_null_undeclared` 指名，子目錄 `_defaults.yaml` 給的值由 `subtree_default_undeliverable` 指名。同一個鍵若列在根目錄的 `optional_overrides:`，租戶給的值照常送出。（`optional_overrides:` 是根目錄 `_defaults.yaml` 頂層的一份鍵名清單：宣告這些閾值存在、但不給平台預設值，租戶自己寫了才送。）

**為什麼不讓 `null` 代表關閉**：YAML 裡 `kx:` 後面留白，解析出來和 `kx: ~` 一樣是 `null`。如果 `null` 代表關閉，打到一半忘了填值就會靜靜關掉一條告警。設定寫錯時，寧可多出告警，也不要少掉告警。

其他鍵寫 `null` 的效果不同：

- **路由的四個欄位**（`_routing` 底下的 `group_by`、`group_wait`、`group_interval`、`repeat_interval`）：寫 `null` 表示不沿用上層的值，產出的 route 不帶這個欄位。寫 `""`、`0` 或 `[]` 效果相同。
- **`_routing.receiver` 不能這樣用**：租戶寫 `receiver: null`，路由產生器會印 WARN 並略過這個租戶，它的告警交回 Alertmanager 的根路由。
- **其他 `_` 開頭的鍵**（例如 `_namespaces`）：在 `_defaults.yaml` 寫 `null` 會刪掉從上層繼承來的值。這只能寫在 `_defaults.yaml` 的 `defaults:` 裡或沒有 `defaults:` 的檔的頂層；租戶檔寫 `null` 會被 `check_confd_schema.py` 擋下。而且 `null` 要和被繼承的值落在同一個位置：

| 上層（有 `defaults:`） | 下層的 `null` 寫在 | 結果 |
|:--|:--|:--|
| `defaults: {_foo: "on"}` | 下層不寫（對照） | 保留 |
| 同上 | 下層 `defaults:` 的**旁邊**（頂層） | 保留：寫了沒有作用 |
| 同上 | 下層 `defaults:` **裡面** | 刪除 |
| 同上 | 下層沒有 `defaults:`，寫在頂層 | 刪除 |

`defaults:` 本身寫成 `null`（`defaults:` 後面留白）時，整份檔當成沒有 `defaults:` 處理。

### 5. `_defaults.yaml` 裡哪些鍵進最終設定

**`defaults:` 底下的鍵進最終設定與 `merged_hash`；根目錄 `_defaults.yaml` 的 `tenants:` 區塊也會進（見下方）。** 子目錄的檔若沒有 `defaults:`，整份文件都當成預設值併入；不過 schema 只允許這種檔的頂層出現固定的鍵，直接寫閾值鍵（例如 `cpu: 80`）會被 `check_confd_schema.py` 擋下。根目錄的 `_defaults.yaml` 一定要有 `defaults:`：沒有時 exporter 不讀其中的閾值，`/effective` 照樣顯示，`not_served` 標 `root_defaults_unwrapped`。

除了 `tenants:` 區塊，與 `defaults:` 並列的頂層鍵不進最終設定，各有自己的讀取程式。改了這些鍵，要到讀它的地方確認有沒有生效：

| 你改的鍵 | 去哪裡確認 |
|:--|:--|
| `state_filters` | `/metrics` 的 `user_state_filter{tenant,filter,severity}` |
| `_routing_defaults`、`_routing_enforced` | `generate_alertmanager_routes.py --config-dir conf.d/ --dry-run` 的完整輸出，改前改後對照 |
| `_custom_alerts` | `compile_custom_alerts.py --check` |
| `max_metrics_per_tenant` | 見下方 |

這張表不是全部。不在表上的鍵，先找到讀它的程式再判斷有沒有生效；頂層其他 `_` 開頭的鍵（例如直接寫在頂層的 `_silent_mode`）沒有讀取者，寫了不生效，也不報錯。

**根目錄 `_defaults.yaml` 的 `tenants:` 區塊**是平台給既有租戶的預設值。它會進最終設定與 `merged_hash`：例如在範例的根目錄加上 `tenants: {fin-db-001: {_silent_mode: warning}}`，`fin-db-001` 的最終設定多出 `_silent_mode: warning`，`key_sources` 標為 `layer: platform`，`platform_overlay` 列出提供它的檔與鍵，`merged_hash` 從 `5db367c3efd997ce` 變成 `73e76f3cabed3a9f`，`/metrics` 也多出 `user_silent_mode{tenant,target_severity}`。同一個鍵由租戶檔勝出，與檔名排序無關；沒有租戶檔宣告的租戶會被忽略並記 WARN（平台檔不能建立租戶）；子目錄平台檔的 `tenants:` 區塊不被讀取，exporter 與路由產生器都記 WARN。

**`max_metrics_per_tenant`** 是每個租戶最多送幾條閾值 series 的上限，只認根目錄 `_defaults.yaml` 的頂層；子目錄 `_defaults.yaml` 或租戶檔寫了會記 WARN 並忽略，租戶因此不能替自己調高上限。未設或 0 時上限是 500，負值表示不截斷。Helm chart 的設定鍵是 `thresholdConfig.max_metrics_per_tenant`。

### 6. 不要把頂層鍵縮排進 `defaults:`

想讓頂層鍵「出現在最終設定裡」而把它縮排進 `defaults:`，結果依值的型別而定，schema 檢查（`check_confd_schema.py`）對兩種都回 `OK`：

- **值不是數字**（對照表、清單、字串）：exporter 丟掉**整份檔**，log 一行 `ERROR: skip unparseable defaults/profiles file …`，`da_config_defaults_unusable{reason="parse_failure"}` 變 1，同檔的其他閾值一起消失。
- **值是數字**：它變成每個租戶的一條閾值 series，沒有任何警告。例如縮排 `max_metrics_per_tenant: 100` 之後，`/metrics` 多出 `user_threshold{component="max",metric="metrics_per_tenant"} 100`。

讀取者只看文件頂層的鍵，縮排後對它們等於沒寫：

- `_routing_defaults` 縮排後，沒有自己 `_routing` 的租戶整條 route 消失，路由產生器回 0、沒有錯誤也沒有警告。
- `_custom_alerts` 縮排後，`compile_custom_alerts.py --check` 回 1 並逐條列出消失的規則；但若接著重新編譯，檢查就轉綠，消失的規則只留在規則包的 diff 裡。

### 7. 雙雜湊與重新載入

| 雜湊 | 怎麼算 | 用途 |
|:--|:--|:--|
| `source_hash` | 租戶檔位元組的 SHA-256，取前 16 個十六進位字元 | 租戶檔有沒有變 |
| `merged_hash` | 最終設定轉成正規化 JSON（鍵排序、無空白）後的 SHA-256，取前 16 個十六進位字元 | 最終設定有沒有變 |

以範例的 `fin-db-001` 對照：

```bash
$ sha256sum conf.d/finance/fin-db-001.yaml | cut -c1-16
45006e7b8bf54ba3
$ printf '%s' '{"pg_locks_count":100,"pg_replication_lag_seconds":30,"pg_stat_activity_count":"150"}' | sha256sum | cut -c1-16
5db367c3efd997ce
```

所以對任何檔案跑 `sha256sum` 都對不上 `merged_hash`。也因為 `merged_hash` 只看最終設定，範例裡根目錄的 `pg_locks_count` 改成什麼值或拿掉，`fin-db-001` 的 `merged_hash` 都還是 `5db367c3efd997ce`：finance 層已經覆蓋了它。

**重新載入的時機**：exporter 每 30 秒（`-reload-interval`）掃描一次 conf.d/。偵測到變動後等 300 毫秒（`-scan-debounce`）再重新載入：這段等待叫去抖動（debounce），把短時間內接連發生的變動合成一次，`git pull` 一次改了 20 個檔也只重新載入一次。

**每次重新載入都重建全部設定。** 兩個雜湊決定的是這次變更記成什麼，供 metrics 與爆炸半徑（blast radius，一次變更影響多少租戶）報告使用：

```
租戶檔的 source_hash 變了            → applied（reason=source；新租戶 reason=new；檔案刪除 reason=delete）
否則，鏈上的 _defaults.yaml 變了：
  merged_hash 變了                    → applied（reason=defaults）
  沒變，且變動的鍵都被租戶自己覆蓋     → shadowed
  沒變，且 defaults: 裡沒有鍵變動      → cosmetic（只改註解、順序、空白，或只改 tenants: 以外的頂層鍵）
```

已知落差：租戶檔那條分支不比對 `merged_hash`，所以只改租戶檔的註解也記成 applied（reason=source）。

| Metric | 類型 | Labels | 說明 |
|:--|:--|:--|:--|
| `da_config_scan_duration_seconds` | histogram | — | 單次掃描耗時 |
| `da_config_reload_trigger_total` | counter | `reason`：`source` / `defaults` / `new` / `delete` | 逐租戶累計記成 applied 的次數 |
| `da_config_defaults_change_noop_total` | counter | — | 記成 cosmetic 的次數 |
| `da_config_defaults_shadowed_total` | counter | — | 記成 shadowed 的次數 |
| `da_config_blast_radius_tenants_affected` | histogram | `reason` / `scope` / `effect` | 每次重新載入受影響租戶數的分布；`effect` 是 `applied` / `shadowed` / `cosmetic` |

雜湊不當 metric label 用，避免 series 數量爆增；`_defaults.yaml` 也不產生自己的 series，它的值算進各租戶的閾值，受 `max_metrics_per_tenant` 限制。

### 8. 頂層鍵的變更，最終設定看不到

除了 `tenants:` 區塊，頂層鍵不進最終設定，所以凡是以最終設定或 `merged_hash` 為輸入的工具都看不到它們的變更：`/effective`、`describe_tenant`、da-guard、爆炸半徑報告、`tenant-verify`。這是為了決策 7 的歸類正確而刻意接受的代價（理由見替代方案 D），但有兩個後果要知道：

- **`effect="cosmetic"` 不代表只改了註解。** 只改根目錄 `_routing_defaults` 的一次變更，與只加一行註解，exporter 都記成 `effect="cosmetic"`。
- **`da-tools tenant-verify --expect-merged-hash` 在這裡不是證據。** 它在雜湊不符時回 2（改根目錄 `tenants:` 區塊會讓它回 2），但改了其他平台頂層鍵之後它照樣回 0：回 0 代表這一面沒被涵蓋，不代表回滾已經驗證。

另建一個比較平台頂層鍵的機制，追蹤在 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)。

### 9. 路由設定沿目錄逐層繼承

路由設定不進最終設定，但沿著同一棵目錄有自己的繼承鏈。以下的結束碼都是 `generate_alertmanager_routes.py` 的；回 2 時什麼都不產生。

**放在哪裡讀**：根目錄從任一 `_` 開頭的檔的頂層讀 `_routing_defaults` 與 `_routing_enforced`。子目錄每一層只讀該層 `_defaults.yaml`（或 `_defaults.yml`）頂層的 `_routing_defaults`；寫在子目錄其他 `_` 檔裡的會被略過並記 WARN，`--validate` 回 1。

**怎麼合併**：各層 `_routing_defaults` 逐頂層鍵淺合併，深層勝出；接著疊上 routing profile，最後是租戶自己的 `_routing`：

```
rd(t)       = 根目錄._routing_defaults ⊕ 第 1 層 ⊕ … ⊕ 租戶所在的那一層
resolved(t) = rd(t) ⊕ profiles[t._routing_profile] ⊕ t._routing

  a ⊕ b：b 的每個頂層鍵整個取代 a 的同名鍵，不往下遞迴
```

例如根目錄給 `receiver` 與 `group_wait: 60s`、`a/_defaults.yaml` 給 `group_wait: 10s`，`a/` 底下的租戶得到根目錄的 `receiver` 與 10s。採淺合併是因為 `receiver` 若深合併，子層把 `type` 從 `slack` 改成 `pagerduty` 時會留下上層的 `api_url`，把兩種 receiver 的欄位混在一起。

**限制**：

- 子目錄的 `_routing_defaults` 不能把 `receiver` 或 `overrides` 寫成 `null`（回 2），否則整個子樹沒有自己 receiver 的租戶都會失去 route。
- `_routing_enforced`（平台強制的路由）只認根目錄，出現在子目錄回 2。目前不支援只作用於某個子樹的強制路由；等到有客戶或團隊明確需要只作用於某個子樹的值班（NOC）路由時再評估。
- routing profile（`_routing_profiles.yaml`）可以放在子目錄，只對該層以下的租戶可見；同一個名稱在整棵樹只能定義一次，重複回 2。
- domain policy（`_domain_policy.yaml`）可以放在子目錄，只約束該子樹；點名子樹外的租戶不生效，加 `--strict` 時是錯誤（回 1），否則記 WARN。不同層的 policy 疊加判定，租戶要同時滿足每一條。
- 同一個租戶 id 宣告在兩個檔，回 1。

### 10. 最終設定與 `/metrics` 不一致時

同一棵樹上，`/effective` 與 `/metrics` 可能給出不同的值，例如子目錄的值根目錄沒宣告、某個檔語法壞掉。決定如下：

1. **最終設定逐字顯示每一層寫下的值，不刪、不改。** `/metrics` 不送的值，在每個租戶的 `not_served` 逐鍵標出原因與所顯示那個值的來源檔。原因是封閉集合，完整清單見 [CLI 參考的 `da-guard effective`](../cli-reference.md#guard)。
2. **原因取自 exporter 載入與解析時自己的判定，不靠比對兩份輸出。** 合法寫法的原文與送出值本來就可能不同（例如 `"60:critical"`），用值比對會誤判。
3. **`not_served` 為空不代表 `/metrics` 送的就是最終設定顯示的值。** 例如租戶覆寫已經過期（`expires:`）時，最終設定顯示原文，`/metrics` 送平台預設，`not_served` 仍是空的。
4. **鏈上語法壞掉的 `_defaults.yaml` 不讓租戶消失。** exporter 不讀那個檔；`/effective` 把它當成空檔，租戶照常回傳並列在 `chain_parse_failed`，`merged_hash` 以略過該檔後的鏈計算。tenant-api 回 HTTP 200，`da-guard effective` 結束碼 3，da-guard 的主檢查也以結束碼 3 停下。
5. **鍵名保留撰寫者的拼法。** 舊拼法（例如 `mysql_cpu`）不算 `not_served`：`/metrics` 以正式名稱送出同一個閾值，兩種拼法的對照在 `da-guard served-values` 的 `aliases`。

## 後果

- **好處**：同一個預設值只寫一次，整棵子樹繼承；預設值變更能逐租戶歸類成 applied / shadowed / cosmetic，爆炸半徑報告看得出一次變更真正影響了誰。
- **代價**：
  - `tenants:` 以外的頂層鍵，變更在最終設定這一面看不到（決策 8）。
  - 只改租戶檔的註解也記成 applied（決策 7）。
  - `_custom_alerts` 在 `describe_tenant.py` 與 Go 實作之間不一致（決策 2）。
  - 沒有 `defaults:` 的子目錄檔整份併入，它的頂層鍵（例如 `state_filters`）因此也進最終設定：改它會讓該子樹每個租戶的 `merged_hash` 都動。`rule-packs/recipes/examples/conf.d/finance/_defaults.yaml` 就是這個形狀（頂層只有 `_custom_alerts`）。

## 考量過的替代方案

### A：只用一個雜湊（只看租戶檔）

❌ 預設值變更時分不出哪些租戶真的受影響，只能把所有租戶都記成受影響，爆炸半徑報告就失去意義。exporter 每次重新載入都重建全部設定，所以雙雜湊省下的不是載入工作，而是正確的歸因。

### B：用檔案系統事件（inotify / fsnotify）取代週期掃描

❌ 在 container 掛載的目錄（ConfigMap 投影卷、NFS、FUSE）上，檔案變動事件不可靠；kernel 也限制每個使用者能掛的監看數（`fs.inotify.max_user_watches`），千租戶的目錄樹可能用完。週期掃描的實測成本見 [benchmarks §1](../benchmarks.md#1-規模能撐多少租戶)。

### C：清單串接而非取代

❌ 上層 `group_by: [severity]`、下層 `group_by: [alertname]`，串接得到 `[severity, alertname]`，語意不明確。寫 `group_by` 的人想的是「換成這個」，不是「再加上這個」。

### D：把其他頂層鍵也併進最終設定

❌ 這是讓決策 8 那些變更看得見最直接的做法，但有三個理由不採用：

1. **歸因失真。** `merged_hash` 決定一次變更記成 applied、shadowed 或 cosmetic。把頂層鍵併進來，每次平台路由編輯都會把每個租戶記成 applied，`da_config_reload_trigger_total{reason="defaults"}` 跟著增加，但實際的載入工作不變（本來就每次全量重建）。
2. **既存的雜湊全部失效。** `merged_hash` 是 `tenant-verify --expect-merged-hash` 的比對值，改變它的定義會讓所有存下來的值對不上。
3. **深合併表達不了路由的語意。** 租戶的 `_routing` 是逐頂層鍵覆蓋 `_routing_defaults`，不是同鍵深合併。改平台的 `group_wait` 時，自帶 `_routing.group_wait` 的租戶 route 完全不變；深合併卻會讓它的 `merged_hash` 移動，被記成受影響。

頂層鍵的變更本來就不依賴 `merged_hash` 生效：只改 `state_filters` 的 severity，`/metrics` 的 `user_state_filter` 跟著變，`merged_hash` 不動；對照組改 `defaults:` 底下的鍵，沒覆蓋它的租戶 `merged_hash` 就會動。

## 影響範圍

- **threshold-exporter**：雙雜湊、去抖動、週期掃描、`da_config_*` 指標。
- **CLI**：`describe_tenant.py` 展開最終設定，`--show-sources` 顯示每個值的來源。
- **tenant-api**：`GET /api/v1/tenants/{id}/effective`。
- **Schema**：`_defaults*.yaml` 由 `platform-defaults.schema.json` 檢查，租戶檔由 `tenant-config.schema.json` 檢查。

## 相關

- [ADR-016: conf.d/ 目錄分層 + 混合模式](016-conf-d-directory-hierarchy-mixed-mode.md)
- [ADR-024: 宣告式 Dimensional 告警引擎](024-version-aware-threshold-via-dimensional-label.md) — `_custom_alerts`
- [CLI 參考：da-guard](../cli-reference.md#guard) — `served-values`、`effective` 與 `not_served` 原因清單
- [Benchmark Report §1 規模](../benchmarks.md#1-規模能撐多少租戶)
- [architecture-and-design.md §設計概念](../architecture-and-design.md#設計概念總覽)
- 未解的問題：[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)（平台頂層鍵的變更比較）、[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)（`_custom_alerts` 兩實作不一致）
