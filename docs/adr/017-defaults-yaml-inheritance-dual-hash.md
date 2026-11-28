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
updated_at: 2026-11-24
---
# ADR-017: _defaults.yaml 多層繼承與雙雜湊熱重載

> **Language / 語言：** **中文 (Current)** | [English](./017-defaults-yaml-inheritance-dual-hash.en.md)

> 與 [ADR-016](016-conf-d-directory-hierarchy-mixed-mode.md)（conf.d/ 目錄分層）為一組：ADR-016 決定設定檔怎麼分目錄放，本篇決定各層的預設值怎麼往下傳，以及預設值改了之後怎麼判斷影響了哪些租戶。

**決策摘要**：conf.d/ 的每一層目錄都可以放一份 `_defaults.yaml`，租戶的設定是從根目錄往下逐層疊上預設值、最後疊上租戶檔的結果；平台出貨即啟用的閾值由讀 conf.d 的程式內建，墊在根目錄之下，客戶的檔案只放覆寫與退出。每個租戶記兩個雜湊：租戶檔本身的，與疊完之後的最終設定的；後者用來判斷一次預設值變更有沒有真的改到這個租戶。

## 狀態

✅ **Accepted**（v2.7.0，2026-04-19）。之後的修訂都已併入下文對應段落：

| 日期 | 修訂內容 | 在本文的位置 |
|:--|:--|:--|
| 2026-04-25 | 「預設值改了、租戶的最終設定沒變」拆成兩種：被租戶覆蓋（shadowed）與沒有實質變更（cosmetic） | 決策 7 |
| 2026-09-28 | 路由設定也沿目錄逐層繼承 | 決策 9 |
| 2026-10-08 | 最終設定逐字顯示寫下的值，`/metrics` 不送的值逐鍵標出 | 決策 10 |
| 2026-10-10 | `describe_tenant.py --show-sources`／`--all` 與 `tenant-verify` 的 `merged_hash` 改讀 da-guard 的值；爆炸半徑報告另比對自訂告警 | 決策 2 |
| 2026-10-17 | 自訂告警編譯器改照 exporter 送出的編譯，`_defaults.yaml` 裡的 `_custom_alerts` 不再產生規則 | 決策 5、6 |
| 2026-10-21 | `describe_tenant` 改為只顯示 exporter 送出的自訂告警清單，爆炸半徑報告退役 | 決策 2 |
| 2026-10-27 | 子目錄 `_defaults.yaml` 不再帶 `_` 開頭的鍵（da-guard 報 error）；租戶這一側把這類鍵寫成 `null` 一律回到平台預設 | 決策 2、4、10 |
| 2026-10-27 | 閾值在每一層同一種讀法（原文讀成數字）：根目錄 `defaults:` 讀不了的純量只拒收那個鍵、不再丟掉整份檔；最終設定與 `merged_hash` 讀到的與 exporter 相同；根目錄 `defaults:` 裡的保留鍵不傳給任何租戶 | 決策 4、6、10 |
| 2026-10-28 | 子目錄才寫、根目錄沒宣告的閾值：da-guard 報 error（結束碼 1） | 決策 3 |
| 2026-11-15 | 平台預設啟用的閾值由讀取端內建，排在根目錄 `_defaults.yaml` 之下：根目錄沒寫、寫 `null` 或寫了讀不了的值，都拿平台值；最終設定與 `merged_hash` 涵蓋它；`validate-config` 印出內建表摘要與每個鍵的處理 | 決策 1、2、3、4、7、11 |
| 2026-11-16 | 根目錄 `defaults:` 可以對內建層給值的鍵寫 `"disable"`，整棵樹關掉那一族；只收這個字 | 決策 4、11 |
| 2026-11-21 | 平台不給值的鍵也進內建層，值是 off：根目錄不必再宣告，租戶的值、`<鍵>_critical` 與 profile 的值都送出 | 決策 11 |
| 2026-11-21 | 根目錄 `defaults:` 的 `<鍵>_critical` 是每個租戶的 critical 預設值，租戶可覆寫；基底沒有值時不送，基底關掉時跟著關 | 決策 3、11 |
| 2026-11-22 | Helm chart 的 `thresholdConfig.defaults` 預設為空，不寫登錄表的鍵；init 也不再寫 `optional_overrides:` 清單 | 決策 11、替代方案 E |
| 2026-11-24 | 閾值過了 `expires:`，改送刪掉那一行時租戶會拿到的值（下面一層的值，沒有就是根目錄的值），`<鍵>_critical` 與維度鍵也適用；下面沒有任何值時照送原值。到了下一個 `expires:` 時刻，檔案沒變也會重新合併 | 決策 7、10、11 |

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

根目錄之下還有一層**內建層**：平台出貨即啟用的閾值與它的平台值，編譯在讀 conf.d 的程式裡，不是 conf.d 裡的檔（決策 11）。所以完整的順序是內建層 → 根目錄 → 往下每一層 → 租戶檔。

### 2. 合併規則

| 值的型別 | 規則 | 例子 |
|:--|:--|:--|
| 對照表（mapping） | 深合併：子層新增的鍵保留，同名鍵由子層覆蓋 | 上層 `{p: 1, q: 1}`、租戶 `{q: 9}` → `{p: 1, q: 9}` |
| 清單（list） | 整個取代，不串接 | 上層 `[ns-a, ns-b]`、租戶 `[ns-c]` → `[ns-c]` |
| 純量（數字、字串） | 子層覆蓋 | 上層 `200`、租戶 `"150"` → `"150"` |

內建層給的值，在 `da-guard effective` 的 `key_sources` 標 `{"layer": "builtin"}`，沒有 `file` 與 `level`。

三個例外：

- **`_metadata` 不繼承**：上層寫的 `_metadata` 不會出現在租戶的最終設定裡。根目錄平台檔的 `tenants.<id>._metadata` 不屬於這條鏈，它與租戶檔的 `_metadata` 逐鍵合併（[#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370)，規則見 [config-driven](../design/config-driven.md)）。
- **子目錄 `_defaults.yaml` 不帶 `_` 開頭的鍵**（路由鍵 `_routing`、`_routing_*` 除外，見決策 9）：詳見決策 4。
- **`_custom_alerts`（租戶自訂告警，見 [ADR-024](024-version-aware-threshold-via-dimensional-label.md)）不跨層合併**：每個租戶只取一處，租戶自己的，否則根目錄平台檔的 `tenants:` 條目，否則綁定的 profile；子目錄 `_defaults.yaml` 的那份標 `not_served`，不送出（見 ADR-024 §3「宣告位置」）。tenant-api、da-guard 與 `describe_tenant.py` 都照這條規則：`describe_tenant.py` 顯示的最終設定就是 da-guard 算的那份，另在最終設定旁列出 `custom_alerts`，也就是 exporter 取得的那份清單裡每一條的判定（名稱、recipe 與狀態：送出 `served`、關閉 `disabled`、拒絕 `rejected`、與前面某條重複 `duplicate`）；標 `not_served` 的那份不在其中。`merged_hash` 也是 da-guard 算的值，與 tenant-api 相同，不隨檔名改變。「這次 conf.d 變更影響了誰」由 config-diff 回答（[ADR-037](037-config-diff-compares-rendered-output.md)）；先前讀 `describe_tenant` 輸出回答同一問題的爆炸半徑報告已退役。

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

`da-guard effective --config-dir conf.d` 的輸出（節錄；`…` 處是內建層其餘的鍵）：

```json
"effective_config": {
  "container_cpu": 80,
  …
  "pg_connections": 80,
  "pg_locks_count": 100,
  "pg_replication_lag": 30,
  "pg_replication_lag_seconds": 30,
  "pg_stat_activity_count": "150",
  …
},
"key_sources": {
  "container_cpu":              {"layer": "builtin"},
  …
  "pg_locks_count":             {"layer": "defaults", "file": "finance/_defaults.yaml", "level": 1},
  "pg_replication_lag_seconds": {"layer": "defaults", "file": "_defaults.yaml", "level": 0},
  "pg_stat_activity_count":     {"layer": "tenant",   "file": "finance/fin-db-001.yaml"},
  …
},
"source_hash": "45006e7b8bf54ba3",
"merged_hash": "78ecf0a647d7b526"
```

`da-guard served-values --config-dir conf.d` 顯示 `/metrics` 送出同樣的值：`pg_locks_count` 100、`pg_replication_lag_seconds` 30、`pg_stat_activity_count` 150，另加內建層的每個鍵。`merged_hash` 涵蓋內建層的值，平台改了內建表它就會跟著變（決策 7、11）。

讀這個範例要注意兩件事：

- **租戶的值加引號，`defaults:` 裡不加。** 租戶的閾值是字串或帶排程的物件，未加引號的數字會被 `check_confd_schema.py` 擋下；平台預設只收數字。
- **子目錄只能改根目錄已宣告的閾值；內建層給值的鍵算已宣告。** 子目錄寫 `pg_connections: 60`，即使根目錄沒寫它，`/metrics` 也送 60。把根目錄的 `pg_locks_count` 拿掉，最終設定仍顯示 100，但 `/metrics` 不送它。`da-guard effective` 的每個租戶有一個 `not_served` 欄位，逐鍵列出「最終設定顯示了、`/metrics` 卻不送」的值與原因，這裡標 `undeliverable`；da-guard 以 `subtree_default_undeliverable` 報錯，結束碼 1。修法是在根目錄 `_defaults.yaml` 或 `optional_overrides:` 宣告這個閾值。子目錄寫的 `<base>_critical` 條件更嚴：基底要在根目錄 `defaults:` 有值，或由內建層給值（包括 off，見決策 11；exporter 只為這種基底送 critical 列），內建層以外的基底只在 `optional_overrides:` 宣告或根目錄寫成 null 都不算，同樣標 `undeliverable`；根目錄 `defaults:` 給了同一個 `_critical` 鍵也不例外：它是租戶沒寫時的 critical 預設值，同樣只在基底有值時送出。

### 4. 閾值寫 `null` 等於這一層沒寫

在租戶檔或子目錄 `_defaults.yaml` 把閾值鍵寫成 `null` 不會關掉告警（根目錄 `defaults:` 寫 `null` 的效果見下方第二條）；要關掉，在這兩處寫 `"disable"`。根目錄 `defaults:` 只對內建層給值的鍵（決策 11）收 `"disable"`，效果是整棵樹關掉那一族，規則見決策 11。其他鍵在根目錄沒有東西可關：根目錄的預設值只收數字，`disable` 是讀不了的值，exporter 只拒收那個鍵、不送它的預設列，同檔其他閾值照送；`check_confd_schema.py` 不會擋，da-guard 以 `root_default_refused` 報錯，結束碼 1。

- 租戶檔或子目錄 `_defaults.yaml` 寫 `null` 時，`/metrics`（`da-guard served-values`）、`/effective`（`da-guard effective`）與 `describe_tenant` 的結果與這一層沒寫這個鍵相同。不過租戶檔寫 `null`（含 `kx:`、`kx: ~`）會被 `check_confd_schema.py` 擋下；租戶要沿用上層的值，就刪掉這個鍵。
- 根目錄 `_defaults.yaml` 的 `defaults:` 寫 `null` 時，根層沒有宣告這個閾值。內建層給值的鍵落到內建值，`/metrics` 送平台值；其他鍵 `/metrics` 不送出這個閾值的 series（不是門檻 0）：租戶給的值由 da-guard 的 `root_default_null_undeclared` 指名，子目錄 `_defaults.yaml` 給的值由 `subtree_default_undeliverable` 指名。同一個鍵若列在根目錄的 `optional_overrides:`，租戶給的值照常送出。（`optional_overrides:` 是根目錄 `_defaults.yaml` 頂層的一份鍵名清單：宣告這些閾值存在、但不給平台預設值，租戶自己寫了才送。）

**為什麼不讓 `null` 代表關閉**：YAML 裡 `kx:` 後面留白，解析出來和 `kx: ~` 一樣是 `null`。如果 `null` 代表關閉，打到一半忘了填值就會靜靜關掉一條告警。設定寫錯時，寧可多出告警，也不要少掉告警。

其他鍵寫 `null` 的效果不同：

- **路由的四個欄位**（`_routing` 底下的 `group_by`、`group_wait`、`group_interval`、`repeat_interval`）：寫 `null` 表示不沿用上層的值，產出的 route 不帶這個欄位。寫 `""`、`0` 或 `[]` 效果相同。
- **`_routing.receiver` 不能這樣用**：租戶寫 `receiver: null`，路由產生器會印 WARN 並略過這個租戶，它的告警交回 Alertmanager 的根路由。
- **其他 `_` 開頭的鍵**（保留鍵，例如 `_silent_mode`、`_severity_dedup`、`_state_<filter>`）：租戶這一側（租戶檔、根目錄平台檔的 `tenants:` 條目、profile）寫 `null`——`kx:`、`kx: ~`，或（`_metadata`、`_routing`、`_routing_defaults`、`_routing_enforced` 以外的鍵）`kx: {default: null}`——會刪掉往下傳來的值，租戶拿到平台預設：`_state_<filter>` 依根目錄 `state_filters` 的 `default_state`，`_severity_dedup` 開啟，`_silent_mode` 不靜音；`/metrics` 與 `/effective` 結果相同。`check_confd_schema.py` 對租戶檔與根目錄平台檔 `tenants:` 條目裡的這類 `null` 多半會擋下，要回到平台預設就刪掉這個鍵；`_severity_dedup: ~` 是例外，schema 允許，意思就是預設的開啟。

**子目錄 `_defaults.yaml` 不帶 `_` 開頭的鍵**（路由鍵 `_routing`、`_routing_*` 除外，由路由產生器判）：寫在子目錄 defaults 裡的 `_state_*`、`_silent_mode`、`_severity_dedup`、`_metadata`、`_profile` 等，exporter 一律不套用，不論值為何。其餘結果取決於兩件事——子目錄寫的是不是 `null`，以及租戶這一側（租戶檔、根目錄平台檔 `tenants:`、profile）對同一鍵寫了什麼：

| 子目錄的值 | 租戶這一側 | da-guard `subtree_default_reserved_key` | `/effective` 的這個鍵 | `not_served` | `/metrics` 送出 |
|:--|:--|:--|:--|:--|:--|
| 非 null | 沒寫 | 報（error） | 不出現 | `value_rejected`，指向子目錄檔 | 平台預設 |
| 非 null | 非 null | 報（error） | 租戶這一側的值 | 不列 | 租戶這一側的值 |
| 非 null | `null` | 報（error） | 不出現 | `value_rejected`，指向子目錄檔 | 平台預設 |
| `null` | 沒寫 | 不報 | 不出現 | 不列 | 平台預設 |
| `null` | 非 null | 不報 | 租戶這一側的值 | 不列 | 租戶這一側的值 |
| `null` | `null` | 不報 | 不出現 | 不列 | 平台預設 |

- 「平台預設」：`_state_<filter>` 依根目錄 `state_filters` 的 `default_state`，`_severity_dedup` 開啟，`_silent_mode` 不靜音，`_metadata` 全空。
- 「`null`」：`kx:` 與 `kx: ~`，對每個 `_` 鍵都是。`kx: {default: null}` 在子目錄 `_defaults.yaml` 對每個鍵都算 `null`；在租戶這一側則不含 `_metadata`、`_routing`、`_routing_defaults`、`_routing_enforced`——這幾個鍵整個 mapping 原樣保留，`default:` 只是 mapping 裡的一個欄位（例：根目錄平台檔 `_platform.yaml` 的 `tenants.t1` 給 `_metadata: {owner: plat}`、租戶檔寫 `_metadata: {default: null}`，送出的 `owner` 仍是 `plat`）。
- `not_served` 那一格的 reason：登錄表列出的鍵是 `value_rejected`，登錄表沒有的 `_` 鍵（例如 `_x`）是 `unregistered`；`file` 是寫了非 null 值的最深一層子目錄檔。這一格只在 `/effective` 不顯示這個鍵時才有：一般是租戶這一側沒有提供它（上表第 1、3 列），`_metadata` 則見下一點。
- `_metadata` 不論哪一層都不出現在 `/effective`（見決策 2），所以子目錄寫了非 null 的 `_metadata` 時，`not_served` 一律列出子目錄檔，租戶自己寫了也一樣；租戶的 `_metadata` 照常送出。
- da-guard 的訊息列出每個寫了該鍵的子目錄檔，以及能寫成一行 YAML 的值（寫不成一行的只列檔）；`_custom_alerts` 另有一句訊息，只列檔、說明該寫在哪裡；登錄表沒有的鍵與根目錄未宣告的 `_state_<filter>` 也各有一句修法。結束碼 1。要讓某些租戶拿到某個值，寫進它們各自的 `tenants:` 條目；`_state_<filter>` 也可以改根目錄的 `default_state`（整棵樹一起變）。
- `merged_hash` 仍照合併規則計算，只在合併結果改變時才動：租戶這一側沒寫、子目錄改了非 null 的值，或兩邊都寫 mapping（逐鍵合併）時會動；租戶這一側寫了純量或 `null`、或鍵是 `_metadata`（合併時略過）時不動。送出的值都不受影響。

例：根目錄 `state_filters: {maintenance: {default_state: enable}}`，`team/_defaults.yaml` 寫 `defaults: {_state_maintenance: disable}`，租戶 `team/t1.yaml` 寫 `tenants: {t1: {}}`：`t1` 的 maintenance filter 照根目錄開啟，`/effective` 沒有 `_state_maintenance`、`not_served` 有 `_state_maintenance: {reason: value_rejected, file: team/_defaults.yaml}`，da-guard 結束碼 1。把 `_state_maintenance: disable` 移到 `t1` 的條目、刪掉子目錄那行之後，filter 關閉，da-guard 結束碼 0。

`defaults:` 本身寫成 `null`（`defaults:` 後面留白）時，整份檔當成沒有 `defaults:` 處理。

### 5. `_defaults.yaml` 裡哪些鍵進最終設定

**`defaults:` 底下的鍵進最終設定與 `merged_hash`。** 根目錄平台檔的 `tenants:` 區塊與根目錄 `_` 開頭檔（通常是 `_profiles.yaml`）的 `profiles:` 區塊也會進（見下方）。子目錄的檔若沒有 `defaults:`，整份文件都當成預設值併入；不過 schema 只允許這種檔的頂層出現固定的鍵，直接寫閾值鍵（例如 `cpu: 80`）會被 `check_confd_schema.py` 擋下。根目錄的 `_defaults.yaml` 一定要有 `defaults:`：沒有時 exporter 不讀其中的閾值，`/effective` 照樣顯示，`not_served` 標 `root_defaults_unwrapped`。

下表這些鍵各有自己的讀取程式，改了不一定反映在 `merged_hash`。改了這些鍵，要到讀它的地方確認有沒有生效：

| 你改的鍵 | 去哪裡確認 |
|:--|:--|
| `state_filters` | `/metrics` 的 `user_state_filter{tenant,filter,severity}` |
| `_routing_defaults`、`_routing_enforced` | `generate_alertmanager_routes.py --config-dir conf.d/ --output-alertmanager --dry-run` 的完整輸出，改前改後對照 |
| `_custom_alerts` | `da-guard served-values` 的 `custom_alerts`（`_defaults.yaml` 裡的 `_custom_alerts` 不送出，也不編譯成規則，見 [ADR-024](024-version-aware-threshold-via-dimensional-label.md) §3） |
| `max_metrics_per_tenant` | 見下方 |

這張表不是全部。不在表上的鍵，先找到讀它的程式再判斷有沒有生效。

**根目錄平台檔的 `tenants:` 區塊**（寫在 `_defaults.yaml` 或其他 `_` 開頭的根目錄檔，例如 `_ops.yaml`；根目錄同時有 `_defaults.yaml` 與 `_defaults.yml` 時只讀前者，後者整份被忽略並記 WARN）是平台給既有租戶的預設值。它會進最終設定與 `merged_hash`：例如在範例的根目錄加上 `tenants: {fin-db-001: {_silent_mode: warning}}`，`fin-db-001` 的最終設定多出 `_silent_mode: warning`，`key_sources` 標為 `layer: platform`，`platform_overlay` 列出提供它的檔與鍵（寫在 `_ops.yaml` 時就標 `_ops.yaml`），`merged_hash` 從 `78ecf0a647d7b526` 變成 `d6f6230d027d11df`，`/metrics` 也多出 `user_silent_mode{tenant,target_severity}`。同一個鍵由租戶檔勝出，與檔名排序無關；沒有租戶檔宣告的租戶會被忽略並記 WARN（平台檔不能建立租戶）；子目錄平台檔的 `tenants:` 區塊不被讀取，exporter 與路由產生器都記 WARN。

**根目錄 `profiles:` 區塊**（通常寫在 `_profiles.yaml`）也會進：租戶寫 `_profile: std` 選用一個 profile 時，profile 的值進入最終設定（`key_sources` 標 `layer: profile`）；改 profile 裡的值時，`key_sources` 把該鍵標為 `layer: profile`、且 `file` 是改的那個檔的租戶 `merged_hash` 跟著變，以 `tenant-verify --expect-merged-hash` 帶入變更前記下的雜湊時回 1；該鍵被租戶檔或根目錄平台檔的 `tenants:` 蓋掉的租戶不受影響。

**`max_metrics_per_tenant`** 是每個租戶最多送幾條閾值 series 的上限，只認根目錄 `_defaults.yaml` 的頂層；子目錄 `_defaults.yaml` 或租戶檔寫了會記 WARN 並忽略，租戶因此不能替自己調高上限。未設或 0 時上限是 500，負值表示不截斷。Helm chart 的設定鍵是 `thresholdConfig.max_metrics_per_tenant`。

### 6. 不要把頂層鍵縮排進 `defaults:`

想讓頂層鍵「出現在最終設定裡」而把它縮排進 `defaults:`，結果依值的型別而定，schema 檢查（`check_confd_schema.py`）對每一種都回 `OK`：

- **值是對照表或清單**：exporter 丟掉**整份檔**，log 一行 `ERROR: skip unparseable defaults/profiles file …`，`da_config_defaults_unusable{reason="parse_failure"}` 變 1，同檔的其他閾值一起消失。
- **值是讀不成數字的純量**（例如 `mysql_connections: abc`）：只拒收那個鍵，同檔的其他閾值照送，da-guard 報 `root_default_refused`。
- **鍵是保留鍵，值是數字或字串**（例如 `_severity_dedup: disable`、`_silent_mode: warning`）：exporter 只從根目錄 `defaults:` 讀閾值，這個值不傳給任何租戶，最終設定卻顯示它是繼承來的（`not_served` 標 `root_defaults_reserved`）。da-guard 報 `root_defaults_reserved_key`（error），`validate-config` 失敗，訊息寫出這個鍵該寫在哪裡。值是對照表或清單時照第一條丟掉整份檔；值是 `null` 時 da-guard 沒有 finding，最終設定也不顯示這個鍵。登錄表沒有的 `_` 鍵（例如 `_myth: 5`）什麼都不會發生，`validate-config` 以 WARN 指出沒有讀取端讀它。
- **值是數字**：它變成每個租戶的一條閾值 series，沒有任何警告。例如縮排 `max_metrics_per_tenant: 100` 之後，`/metrics` 多出 `user_threshold{component="max",metric="metrics_per_tenant"} 100`。

讀取者只看文件頂層的鍵，縮排後對它們等於沒寫：

- `_routing_defaults` 縮排後，沒有自己 `_routing`、也沒有 routing profile 的租戶整條 route 消失，路由產生器回 0、沒有錯誤也沒有警告。
- `_custom_alerts` 本身縮排前後都不送出、也不編譯成規則：自訂告警編譯器照 exporter 送出的編譯。

### 7. 雙雜湊與重新載入

| 雜湊 | 怎麼算 | 用途 |
|:--|:--|:--|
| `source_hash` | 租戶檔位元組的 SHA-256，取前 16 個十六進位字元 | 租戶檔有沒有變 |
| `merged_hash` | 最終設定轉成正規化 JSON（鍵排序、無空白）後的 SHA-256，取前 16 個十六進位字元 | 最終設定有沒有變 |

以範例的 `fin-db-001` 對照（第二條指令把最終設定排序鍵、去掉空白再算雜湊）：

```bash
$ sha256sum conf.d/finance/fin-db-001.yaml | cut -c1-16
45006e7b8bf54ba3
$ da-guard effective --config-dir conf.d | jq -jcS '.tenants["fin-db-001"].effective_config' | sha256sum | cut -c1-16
78ecf0a647d7b526
```

所以對任何檔案跑 `sha256sum` 都對不上 `merged_hash`。也因為 `merged_hash` 只看最終設定，範例裡根目錄的 `pg_locks_count` 改成什麼值或拿掉，`fin-db-001` 的 `merged_hash` 都還是 `78ecf0a647d7b526`：finance 層已經覆蓋了它。

內建層的值也在最終設定裡：平台改了內建表的某個值，沒覆寫那個鍵的租戶 `merged_hash` 跟著變，這正是它要回答的問題。根目錄對每個內建鍵都寫了與平台相同數字的樹，加不加內建層，每個租戶的最終設定與 `merged_hash` 都相同。

**重新載入的時機**：exporter 每 30 秒（`-reload-interval`）掃描一次 conf.d/。偵測到變動後等 300 毫秒（`-scan-debounce`）再重新載入：這段等待叫去抖動（debounce），把短時間內接連發生的變動合成一次，`git pull` 一次改了 20 個檔也只重新載入一次。

檔案沒變也會重新載入：設定是依當下時刻合併的，過了 `expires:` 的閾值等於沒寫（決策 10）。所以到了樹裡下一個 `expires:` 的時刻，exporter 經同樣的去抖動後重新合併；時鐘被調回到上一個已過的 `expires:` 之前時，下一次掃描也會重新合併。重新合併一直沒提交時（例如這次載入被拒），exporter 照送上一版的答案，已過期的值也照送，和其他任何凍結的設定一樣；`da_config_valid_until_unixtime_seconds` 停在過去的那一刻，超過 5 分鐘就響告警 `ConfigExpiryRebuildOverdue`。

**每次重新載入都重建全部設定。** 兩個雜湊決定的是這次變更記成什麼，供 metrics 使用，包括受影響租戶數（`da_config_blast_radius_tenants_affected`，一次變更影響多少租戶）：

```
租戶檔的 source_hash 變了            → applied（reason=source；新租戶 reason=new；檔案刪除 reason=delete）
否則，鏈上的 _defaults.yaml 變了：
  merged_hash 變了                    → applied（reason=defaults）
  沒變，且變動的鍵都被租戶自己覆蓋     → shadowed
  沒變，且 defaults: 裡沒有鍵變動      → cosmetic（例如只改註解、順序、空白，或只改根目錄 _defaults.yaml 裡的 _routing_defaults）
```

根目錄平台檔 `tenants:` 區塊裡某個租戶的條目改了，也走 reason=defaults 這一支：該租戶的 `merged_hash` 變了就記 applied，改的鍵被租戶檔覆蓋就記 shadowed。

已知落差：租戶檔那條分支不比對 `merged_hash`，所以只改租戶檔的註解也記成 applied（reason=source）。

| Metric | 類型 | Labels | 說明 |
|:--|:--|:--|:--|
| `da_config_scan_duration_seconds` | histogram | — | 單次掃描耗時 |
| `da_config_reload_trigger_total` | counter | `reason`：`source` / `defaults` / `new` / `delete` | 逐租戶累計記成 applied 的次數 |
| `da_config_defaults_change_noop_total` | counter | — | 記成 cosmetic 的次數 |
| `da_config_defaults_shadowed_total` | counter | — | 記成 shadowed 的次數 |
| `da_config_blast_radius_tenants_affected` | histogram | `reason` / `scope` / `effect` | 每次重新載入受影響租戶數的分布；`effect` 是 `applied` / `shadowed` / `cosmetic` |

雜湊不當 metric label 用，避免 series 數量爆增；`_defaults.yaml` 也不產生自己的 series，它的值算進各租戶的閾值，受 `max_metrics_per_tenant` 限制。

### 8. 有些頂層鍵的變更，最終設定看不到

例如根目錄的 `_routing_defaults` 與 `state_filters` 不進最終設定，改了之後 `merged_hash` 不動，所以凡是以最終設定或 `merged_hash` 為輸入的工具都看不到這些變更：`/effective`、`describe_tenant`、`tenant-verify`。這是為了決策 7 的歸類正確而刻意接受的代價（理由見替代方案 D），但有兩個後果要知道：

- **`effect="cosmetic"` 不代表只改了註解。** 只改根目錄 `_defaults.yaml` 裡 `_routing_defaults` 的一次變更，與只加一行註解，exporter 都記成 `effect="cosmetic"`。
- **`da-tools tenant-verify --expect-merged-hash` 在這裡不是證據。** 它在雜湊不符時回 1，但只改根目錄 `_routing_defaults` 之後它回 0：回 0 代表這一面沒被涵蓋，不代表回滾已經驗證。

另建一個比較這類平台頂層鍵的機制，追蹤在 [#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)。

### 9. 路由設定沿目錄逐層繼承

根目錄的 `_routing_defaults` 不進最終設定（租戶自己的 `_routing` 會進），但路由設定沿著同一棵目錄有自己的繼承鏈。以下的結束碼都是 `generate_alertmanager_routes.py` 的；回 2 時什麼都不產生。

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

1. **最終設定逐字顯示每一層寫下的值，不刪、不改。** 閾值若被 YAML 的型別讀成另一個數，顯示原文，因為 exporter 讀的就是原文：子目錄寫 `017`，最終設定顯示 `"017"`、`/metrics` 送 17；寫 `0x1F`，最終設定顯示 `"0x1F"` 並標 `value_rejected`，`/metrics` 改送上一層的值。`/metrics` 不送的值，在每個租戶的 `not_served` 逐鍵標出原因與所顯示那個值的來源檔。唯一的例外是子目錄 `_defaults.yaml` 寫的 `_` 開頭鍵：兩邊都不套用，最終設定不顯示子目錄那份，何時列進 `not_served` 見決策 4 的表。原因是封閉集合，完整清單見 [CLI 參考的 `da-guard effective`](../cli-reference.md#guard)。
2. **原因取自 exporter 載入與解析時自己的判定，不靠比對兩份輸出。** 合法寫法的原文與送出值本來就可能不同（例如 `"60:critical"`），用值比對會誤判。
3. **`not_served` 為空不代表 `/metrics` 送的就是最終設定顯示的值。** 例如租戶覆寫已經過期（`expires:`）時，最終設定顯示原文，`/metrics` 送的是刪掉那一行時租戶會拿到的值（下面一層的值，沒有就是根目錄的值），`not_served` 仍是空的。
4. **鏈上語法壞掉的 `_defaults.yaml` 不讓租戶消失。** exporter 不讀那個檔；`/effective` 把它當成空檔，租戶照常回傳並列在 `chain_parse_failed`，`merged_hash` 以略過該檔後的鏈計算。tenant-api 回 HTTP 200，`da-guard effective` 結束碼 3，da-guard 的主檢查也以結束碼 3 停下。
5. **鍵名保留撰寫者的拼法。** 舊拼法（例如 `mysql_cpu`）不算 `not_served`：`/metrics` 以正式名稱送出同一個閾值，兩種拼法的對照在 `da-guard served-values` 的 `aliases`。

### 11. 內建層：平台預設啟用的閾值由讀取端自己帶著

閾值登錄表（`rule-packs/threshold-registry.yaml`，平台所有閾值鍵的唯一來源）把平台出貨即啟用的鍵標為 `arming: default`。這些鍵與它們的平台值編譯在 exporter、da-guard 與 tenant-api 裡（三者共用同一套 Go 程式庫，`da-tools validate-config` 經 da-guard 取得答案），排在根目錄 `_defaults.yaml` 之下，叫做內建層。登錄表標為 `operator`（平台不主張值、要部署方自己校準）的鍵也在內建層，值是 off（見下方「平台不給值的鍵」）；`_critical` 鍵不在內建層。

**為什麼**：在此之前，平台的預設值要由 `da-tools init` 連數字複製進客戶的根目錄 `_defaults.yaml`。那份檔案之後歸客戶所有，平台新增的預設啟用鍵與重新校準的值都進不來，對應的告警不會響，也沒有錯誤訊息。Helm chart 的值隨 chart 升級、客戶只寫差異，conf.d 這一側沒有同樣的機制。現在客戶的樹只放覆寫與退出，預設值跟著讀取端出貨：`da-tools init` 與 `da-tools scaffold` 產生的根 `_defaults.yaml` 不寫這些鍵，註解只說明下表的寫法、不列數字（寫進註解的數字一樣會過期；現值由 `da-tools validate-config` 的 `platform_defaults` 列回答）。既有的樹裡與內建值相同的數字是複製品，那一列會註明；`da-tools migrate-builtin-defaults` 刪掉它們，判斷一個值是不是複製品的方式是刪掉後各租戶送出的列不變。

**一條規則決定它的所有行為：內建層給的值，效果等同把同一個數字寫在根目錄 `defaults:`，差別只有來源標示。** 根目錄給了數字的鍵用根目錄的數字；根目錄沒寫、寫 `null`（決策 4）或寫了讀不了的值（da-guard 照舊以 `root_default_refused` 報錯）的鍵，都拿內建值。所以凡是「根目錄 `defaults:` 有值」的既有規則，內建層給值的鍵一律適用：子目錄與租戶可以覆寫它、可以寫 `"disable"` 關掉它；租戶寫的 `<鍵>_critical` 會送出 critical 列；租戶寫這個鍵不會被判為沒有讀取端讀（[ADR-038](038-unread-keys-judged-by-readers.md) 決定 7）；`"disable"` 過期（`expires:`）後失效，基底列回來。

| 想要的結果 | 寫法 |
|:--|:--|
| 用平台值，並跟著平台之後的校準 | 不寫這個鍵 |
| 整個平台用自己的數字（也等於釘住，不跟平台變動） | 根目錄 `defaults:` 寫 `<鍵>: 70` |
| 整個平台預設不要這條告警（子目錄、profile 或租戶給了值仍會開回） | 根目錄 `defaults:` 寫 `<鍵>: "disable"` |
| 某一層子目錄或某個租戶不要這條告警 | 子目錄 `_defaults.yaml` 或租戶檔寫 `<鍵>: "disable"` |
| 某個租戶用自己的數字 | 租戶檔寫 `<鍵>: "70"` |

根目錄檔的狀態決定內建層給不給：

| 根目錄 `_defaults.yaml` | 內建層 |
|:--|:--|
| 不存在，或 `defaults:` 是空的 | 給值。這是合法的樹 |
| 讀不了：語法錯，或檔案存在卻讀不到（例如 symlink 指向不存在的檔） | 不給值，整棵樹不送任何預設列（da-guard 結束碼 3）。根目錄讀不了時，寫在裡面的覆寫與退出也讀不到；這時送出平台值，等於替客戶打開他關掉的告警 |

內建層對每個租戶都給值，不看部署裡有沒有裝對應的規則包：多一列不等於多一條告警，告警要響還需要規則包已部署、指標存在。Helm chart 的 `thresholdConfig.defaults` 預設是空的，不渲染任何登錄表的鍵，所以 chart 與 exporter 映像要一起升級；部署方在那裡寫的數字，在 ConfigMap 裡算根目錄寫的數字。

**平台不給值的鍵：內建層給 off。** 登錄表標 `operator` 的鍵在內建層的值是 off：它算「根目錄 `defaults:` 有值」，所以上面那條規則照樣適用——租戶、profile、子目錄給的值都送出，租戶寫的 `<鍵>_critical` 也送 critical 列，即使租戶沒給基底值；但 off 本身不送基底列，也不關掉整族。客戶的根目錄因此不必再把這些鍵寫進 `defaults:` 或列在 `optional_overrides:`，租戶才寫得動它們。根目錄給數字照舊蓋過 off，寫 `null` 等於沒寫（仍是 off，租戶的值照送），寫 `"disable"` 照下方「根目錄的 `"disable"`」整族關閉。根目錄 `optional_overrides:` 還列著這些鍵也無妨：列名對內建層裡的鍵是多餘的，不擋 profile 給值。兩點與有數字的鍵不同：這些鍵的 `expires:` 只在下面還有一層給值時生效（平台檔的 `tenants:`、profile 或子目錄的 `_defaults.yaml`，過期後送那一層的值）；下面沒有任何一層給值時，過期等於退回 off，告警會悄悄消失，所以值過了 `expires:` 照送，tenant-api 寫入時擋下，已在磁碟上的由 exporter 每次載入設定時記一行 WARN；`validate-config` 的 `platform_defaults` 列把它們按規則包分行列出，附設定了門檻或 `disable` 的租戶數（`0/N` 表示這個鍵的告警對所有租戶都不會響）。

例：根目錄 `_defaults.yaml` 只寫 `defaults: {jvm_threads: "disable"}`，租戶檔：

```yaml
tenants:
  t1:
    redis_connected_clients: "200"
    redis_replication_lag_critical: "60"
    jvm_threads: "500"
  t2: {}
```

`da-guard served-values` 給 `t1` 的 redis 與 jvm 鍵（節錄）：`redis_connected_clients` 200、severity `warning`；`redis_replication_lag_critical` 60、severity `critical`（基底沒有值，critical 照送）；`jvm_threads` 500（根目錄整族關閉，租戶給了值就回來）。`t2` 這兩個規則包的鍵一列都沒有。da-guard 結束碼 0。

**根目錄的 `"disable"`**：「關掉」在每一層都是同一個寫法。根目錄寫 `<鍵>: "disable"`，每個租戶都繼承它，與子目錄 `_defaults.yaml` 寫 `"disable"` 的效果相同，只是往上一層：整族關閉（基底、`<鍵>_critical`、維度鍵，[ADR-038](038-unread-keys-judged-by-readers.md) 決定 8）；子目錄、profile 或租戶自己給了值，那個範圍的整族就回來，包括租戶寫的 `<鍵>_critical`；租戶自己的 `"disable"` 過期後，落回繼承來的根目錄 `"disable"`，不是平台值。租戶檔裡的 `<鍵>_critical` 或維度鍵因此被關掉時，da-guard 的鍵判決報 `disable_cascade` 警告，不擋。`validate-config` 的 `platform_defaults` 列把這個鍵標為 `opted_out`。

根目錄的 `"disable"` 比子目錄嚴格：

- **只收 `disable` 這個字**（大小寫不拘）。子目錄把 `false`、`off`、`disabled` 也讀成停用；根目錄收這些別名的話，`helm --set …=false` 會靜靜關掉整棵樹的一族。在根目錄它們是讀不了的值：da-guard 以 `root_default_refused` 報錯（結束碼 1），該鍵照送平台值。
- **不收排程與 `expires:`**。根目錄的值只能是純量；寫成排程的根目錄檔整份讀不了，內建層撤掉（上表）。限時關閉寫在子目錄或租戶檔。
- **只對內建層裡的鍵有效**（有數字的鍵與 off 的鍵）。其他鍵照決策 4 是讀不了的值。
- **根目錄不得同時給同一族的值**。根目錄寫了 `<鍵>: "disable"`，又給 `<鍵>{…}` 值時，那個值跟著基底一起關掉，送不到任何沒自己給基底值的租戶；又給 `<鍵>_critical` 值時，那個 critical 預設值也跟著基底關掉，同樣送不到任何租戶。兩種都是自相矛盾，da-guard 以 `root_disable_family_value` 報錯，結束碼 1。

根目錄的 `optional_overrides:`（宣告「平台認得這個鍵、但不給值」的鍵名清單）不影響內建層。根目錄 `defaults:` 沒寫這個預設啟用鍵（或寫 `null`）卻把它列在裡面時，da-guard 以 `root_optional_builtin` 報錯，結束碼 1：列名關不掉這條告警，要整棵樹關掉就在根目錄 `defaults:` 寫 `"disable"`。

例：`conf.d/_defaults.yaml` 寫 `defaults: {redis_connected_clients: 1000}`，`conf.d/t1.yaml` 寫：

```yaml
tenants:
  t1:
    pg_connections: "70"
    pg_replication_lag_critical: "60"
    container_cpu: "disable"
```

`da-guard served-values --config-dir conf.d` 給 `t1` 的值（節錄）：`pg_connections` 70（租戶覆寫）、`pg_replication_lag` 30（內建層）、`pg_replication_lag_critical` 60、severity `critical`（基底有內建值，租戶的 critical 送出）、`redis_connected_clients` 1000（根目錄），以及內建層其餘的每個鍵；`container_cpu` 被租戶的 `"disable"` 關掉，沒有列。

全平台退出：在同一個根目錄 `defaults:` 加一行 `pg_replication_lag: "disable"`，`t1` 的 `pg_replication_lag` 與 `pg_replication_lag_critical` 兩列消失（租戶的 critical 跟著基底一起關），其餘的列不變，da-guard 結束碼 0。

**內建表怎麼產生**：`scripts/tools/lint/check_threshold_registry.py --regen` 從登錄表生成 Go 原始碼（`components/threshold-exporter/app/pkg/config/builtin_defaults_gen.go`），三個讀取端從同一份原始碼編譯；登錄表改了沒有重新生成時，`--ci` 檢查失敗。這張表以摘要命名：有數字的鍵每個一行 `鍵=值`、off 的鍵每個一行 `鍵=off`，依鍵排序後的 SHA-256 前 12 個十六進位字元。

**改動內建表是 breaking change。** 改值、新增或移除預設啟用鍵，都會在客戶的 repo 沒有任何 diff 的情況下改變告警行為。所以 `check_threshold_registry.py --ci` 要求新表的摘要出現在一份標 `⚠️ breaking` 的 `changelog.d/` 片段裡（或已發布的 `CHANGELOG.md`），摘要變了，舊片段就不算數。

**以摘要對齊，不以版號。** exporter、da-tools（內含 da-guard）、tenant-api 各有自己的發布線與版號，規則包沒有自己的發布線，「同一版」沒有可以比對的東西；兩個元件的內建表摘要相同，就是對齊。元件之間的落差依方向有不同後果，都不會報錯（偵測見下方的執行期訊號）：

| 落差 | 結果 |
|:--|:--|
| 規則包的登錄表比 exporter 新 | 新的預設啟用鍵沒有內建值，告警不響 |
| exporter 比規則包新 | 新增的鍵多出沒有規則讀的列；重新校準過的值由舊規則照讀 |
| CI 的 da-guard 或 tenant-api 比部署中的 exporter 新 | 靠內建層的樹或寫入在檢查端通過，exporter 卻不送那些列（[ADR-036](036-single-parser-effective-config.md) §6） |

**摘要與每個鍵的處理在哪裡讀得到**：`da-tools validate-config` 的 `platform_defaults` 列第一行印出 da-tools 帶的內建表摘要，之後每個預設啟用鍵一行，寫出這棵樹怎麼處理它：`builtin`（根目錄沒給值，由內建層給平台值，附 `/metrics` 送這個值的租戶數）、`overridden`（根目錄給了數字，附那個數與平台值）或 `opted_out`（根目錄寫了 `"disable"`）；再來每個規則包一行，列出 off 的鍵（見上方「平台不給值的鍵」）。答案取自 `da-guard effective` 輸出的 `builtin_defaults`，validate-config 不另讀登錄表。

**執行中的元件送出的訊號**：exporter 送 `da_config_builtin_defaults_info{digest}`（它編譯進去的表的摘要），以及每個預設啟用鍵的 `da_config_builtin_default_state{key,state}`，不帶租戶標籤；`state` 除了上面三種，還有 `withheld`（根目錄 `_defaults.yaml` 讀不了，內建層撤掉）。每個鍵的狀態與 `da-guard effective` 的 `builtin_defaults` 出自同一個推導。tenant-api 送 `tenant_api_builtin_defaults_info{digest}`。平台規則包帶一條記錄規則 `rule_pack_builtin_defaults_info{rulepack_digest}`，是生成它的那份登錄表的摘要（`--regen` 寫入，`--ci` 在它過期時失敗），並出貨告警 `BuiltinDefaultsDigestMismatch`：被 scrape 的 exporter 或 tenant-api 的摘要與它不同、或沒送摘要（內建層之前的版本，標為 `digest="none"`），持續 30 分鐘就觸發（依元件與摘要分組，pod 換了 IP 不重算）。告警文字寫出兩邊的摘要，並依上表說明哪一邊較新會怎樣。同一個 Prometheus 載入了兩份摘要不同的平台規則包（例如舊的 ConfigMap 與新的 PrometheusRule 並存）時，上一條對符合任一份的元件不響，改由 `BuiltinDefaultsRulePackDigestsDiffer` 觸發。⚠️ 這條告警只隨平台規則包出貨：threshold-exporter 的 Helm chart 不出貨規則包，沒有部署平台規則包的環境沒有這條告警，要自己比對這兩個指標與 `da-guard effective` 的摘要。

升級與回退時要注意同一件事：還沒有內建層的舊版讀取端，讀到一棵只放覆寫的樹，那些鍵就沒有值。所以要先把讀取端都升級完，才刪掉根目錄裡與平台相同的數字；要回退到舊版，先把它們放回去。根目錄的 `"disable"` 也不能早於升級：已發布的舊版 exporter 讀到它會丟掉整份根目錄 `_defaults.yaml`，所以要和升級讀取端放在同一個變更裡寫。

## 後果

- **好處**：同一個預設值只寫一次，整棵子樹繼承；預設值變更能逐租戶歸類成 applied / shadowed / cosmetic，受影響租戶數的 metrics 看得出一次變更真正影響了誰。
- **代價**：
  - 根目錄 `_routing_defaults`、`state_filters` 這類頂層鍵的變更，在最終設定這一面看不到（決策 8）。
  - 只改租戶檔的註解也記成 applied（決策 7）。
  - 沒有 `defaults:` 的子目錄檔整份併入，它的頂層鍵（例如 `state_filters`）因此也進最終設定：改它會讓該子樹每個租戶的 `merged_hash` 都動。
  - **省略一個鍵的意思是「用平台值」**（決策 11）。平台改了內建表，會在客戶沒有 diff 的情況下改變告警；不接受的客戶把數字寫進根目錄，就釘住了。
  - 元件之間的內建表落差只能靠比對摘要發現，目前沒有任何元件或告警替你比對（決策 11 的落差表）。
  - 內建層多出的列計入 `max_metrics_per_tenant`。超過上限時 exporter 截斷，先被截掉的可能是租戶自己寫的列；da-guard 以 `cardinality_exceeded` 指名這些租戶，結束碼 1。
  - exporter 不把「讀不了的檔出現或消失」當成變更：根目錄多出一個讀不了的 `_defaults.yml` 時，exporter 要到下一次有其他檔變動才重新載入、撤掉內建層，da-guard 與 tenant-api 則立刻反映。

## 考量過的替代方案

### A：只用一個雜湊（只看租戶檔）

❌ 預設值變更時分不出哪些租戶真的受影響，只能把所有租戶都記成受影響，受影響租戶數的 metrics 就失去意義。exporter 每次重新載入都重建全部設定，所以雙雜湊省下的不是載入工作，而是正確的歸因。

### B：用檔案系統事件（inotify / fsnotify）取代週期掃描

❌ 在 container 掛載的目錄（ConfigMap 投影卷、NFS、FUSE）上，檔案變動事件不可靠；kernel 也限制每個使用者能掛的監看數（`fs.inotify.max_user_watches`），千租戶的目錄樹可能用完。週期掃描的實測成本見 [benchmarks §1](../benchmarks.md#1-規模能撐多少租戶)。

### C：清單串接而非取代

❌ 上層 `group_by: [severity]`、下層 `group_by: [alertname]`，串接得到 `[severity, alertname]`，語意不明確。寫 `group_by` 的人想的是「換成這個」，不是「再加上這個」。

### D：把 `_routing_defaults`、`state_filters` 這類頂層鍵也併進最終設定

❌ 這是讓決策 8 那些變更看得見最直接的做法，但有三個理由不採用：

1. **歸因失真。** `merged_hash` 決定一次變更記成 applied、shadowed 或 cosmetic。把它們併進來，每次平台路由編輯都會把每個租戶記成 applied，`da_config_reload_trigger_total{reason="defaults"}` 跟著增加，但實際的載入工作不變（本來就每次全量重建）。
2. **既存的雜湊全部失效。** `merged_hash` 是 `tenant-verify --expect-merged-hash` 的比對值，改變它的定義會讓所有存下來的值對不上。
3. **深合併表達不了路由的語意。** 租戶的 `_routing` 是逐頂層鍵覆蓋 `_routing_defaults`，不是同鍵深合併。改平台的 `group_wait` 時，自帶 `_routing.group_wait` 的租戶 route 完全不變；深合併卻會讓它的 `merged_hash` 移動，被記成受影響。

這類鍵的變更本來就不依賴 `merged_hash` 生效：只改 `state_filters` 的 severity，`/metrics` 的 `user_state_filter` 跟著變，`merged_hash` 不動；對照組改 `defaults:` 底下的鍵，沒覆蓋它的租戶 `merged_hash` 就會動。

### E：內建層的其他做法

- **維持由 init 複製預設值，只靠檢查點名缺的鍵**：訊號只給有跑檢查的部署，而且每次平台新增預設啟用鍵，每個客戶都要手動補一次。
- **讓 init 重跑來更新**：`init --force` 會重寫它產生的每一個檔，丟掉所有手改。
- **登錄表隨規則包出貨、exporter 在執行期讀檔**：值與讀它的規則永遠同版，落差消失；但 CI 裡的 da-guard 與 tenant-api 手上沒有那個檔，三個讀取端的答案會不同，違反「同一棵樹只有一個答案」（ADR-036）。
- **只在組裝 ConfigMap 時注入預設值**：走既有 ConfigMap 的部署與 tenant-api 的 Git 工作目錄拿不到，讀取端之間不一致。
- **用 `optional_overrides:` 列名當退出**：退出之後的鍵照那個欄位的規則走（租戶把基底開回來時 `_critical` 不送、profile 給的值不套用）；而且這份清單一旦寫在客戶的樹裡，日後平台把清單上的鍵升為預設啟用時，既有的樹會把它讀成已退出，也沒有錯誤。
- **讓根目錄的 `null` 代表退出**：理由同決策 4，留白的鍵不該靜靜關掉告警。
- **只對部署了對應規則包的租戶給值**：exporter 不知道部署了哪些規則包，只能從樹的內容推論，推論有已知的誤判。
- **升級時自動刪掉客戶檔裡與平台相同的數字**：那個數字可能是刻意釘住的，刪掉之後它會跟著平台變動，這該由客戶決定。

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
- 未解的問題：[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)（平台頂層鍵的變更比較）
