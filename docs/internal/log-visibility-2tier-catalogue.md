---
title: "2-Tier 日誌可見度 Catalogue（ADR-021 Phase 1 / #609）"
tags: [internal, governance, federation, logs, multi-tenant, visibility]
audience: [platform-engineers, sre]
version: v2.9.0
lang: zh
---

# 2-Tier 日誌可見度 Catalogue（ADR-021 Phase 1 / #609）

> 本文件是 ADR-021 [實作計畫 item 6](../adr/021-tenant-log-query-federation.md)
> 「2-tier log visibility policy schema + 可見度 catalogue」的落地，與
> [平台日誌彙整 runbook §8](platform-log-aggregation-runbook.md) 互為一體（runbook
> 講 (b) 投影**怎麼運作**；本文件講「**哪些 stream / field 對租戶可見**」的策展邊界）。
>
> **受眾**：平台 maintainer / SRE。租戶端操作者請改看
> [租戶日誌查詢 onboarding 指南](../integration/tenant-log-query.md)。

## ⛔ 先讀：這是「控制平面策展」、不是「查詢期硬阻擋」

ADR-021 [§治理邊界](../adr/021-tenant-log-query-federation.md) 把**安全邊界**與**治理邊界**刻意分開，本 catalogue 屬於後者：

| | 機制 | 在哪強制 |
|---|---|---|
| **安全邊界（資料平面）** | 跨租戶隔離 100% 來自 **VictoriaLogs 原生 `(AccountID, ProjectID)`** + Vector ingest-time **allowlist 淨化**（寫進租戶分區前從零重建 event）| `helm/vector`（淨化）+ `helm/federation-gateway` victorialogs mode（gateway 注入已驗證 `AccountID` header）|
| **治理邊界（控制平面）= 本 catalogue** | 「哪些平台 log stream / field **策展為**對租戶可見」的**人類可讀目錄**——**不在 query path 硬擋** | 文件 + 對 enforced 設定的 **drift-guard 測試** |

> ⛔ **本 catalogue 不是 runtime filter，也不要被實作成 runtime filter。** 租戶
> 在平台上**結構性**只看得到自己 `AccountID` 分區裡、且已被 Vector 淨化過的列——
> 這層硬隔離由資料平面擔保（見上表左欄），catalogue **不重複**做一次過濾。catalogue
> 的角色是：把「資料平面**事實上**讓租戶看到什麼」寫成 maintainer 看得懂、可審查、可
> 隨 onboarding 對照的策展目錄，並用測試釘住它**不漂離** enforced 設定。

## SSOT：catalogue 引用 enforced 設定，不雙寫欄位清單

「租戶可見的欄位子集」這份清單**只有一份權威來源**——`helm/vector/values.yaml` 的
**`tenantProjectionKeepFields`**（fail-closed allowlist：租戶分區的 event 從零重建、**只**
含這些欄位 + 結構性注入的 `account_id`/`log_event_id`/`timestamp`）。

> ⛔ **本 catalogue 不另列一份平行的 field allowlist YAML。** 若再寫一份，兩份會
> drift（改了 `values.yaml` 忘了改 catalogue → 文件騙人；改了 catalogue 以為改了
> enforcement → 其實沒有）。catalogue **引用** `tenantProjectionKeepFields` 為 SSOT，
> 並由 `tests/dx/test_log_visibility_catalogue.py` 斷言「本文件 §Tier-2 表列出的欄位集
> == `values.yaml` enforced 的 `tenantProjectionKeepFields`」——任一邊改了沒同步另一邊，
> 測試紅燈。新增/移除租戶可見欄位＝改 `values.yaml`（security-reviewed 動作），**再**同步
> 本表（測試逼你同步）。

### 結構性注入欄位（template 注入、非 `tenantProjectionKeepFields` 列舉）

`tenant_project` 從零重建租戶 event 時，除了 copy `tenantProjectionKeepFields` 的欄位，
template 還**結構性注入**下列欄位（故它們出現在 §Tier-2 表、但 `values.yaml` 不列舉它們為 keep-field）。
⛔ drift-guard 測試從**本小節**讀這份注入清單（**非測試碼硬編碼常數**）——故未來新增注入欄位
（如 Phase 2 (a) 的 `project_id`，或任何平台級欄位）**必須在此登記**，測試才會把它從 §Tier-2 ↔ keepFields
的 set-equality 排除；忘了登記就紅燈，杜絕「偷偷加進測試白名單卻沒進目錄」的防線漏洞。

| 注入欄位 | 來源 / 為何不在 keepFields |
|---|---|
| `account_id` | `tenant_project` 從 Git registry map 注入的**可信分區 key**（`kept.account_id = aid`，**永不**取自 payload）；values 不列舉、由 template 注入 |
| `log_event_id` | `demux` 階段注入的跨分區 join key（UUIDv7）；同時亦是 keepField（loop 會 copy），列此處標明其 template-注入本質 |
| `timestamp` | Vector event time（sink `_time_field`）；同時亦是 keepField，列此處標明 template-注入本質 |

## Tier-1 — Platform-exposable streams（maintainer 策展：哪些 stream 可策展給租戶）

平台完整 log 落 `0:0`（跨租戶 audit view，平台 ops 自用）。其中**只有**下列 stream class
**有資格**被投影成租戶可見副本——其餘 stream class **永遠 platform-only**（連淨化副本都不產）。
此 tier-1 由 Vector `tenant_route` 的「帶有效 `tenant_id` 的 `federation_audit` 列才投影」邏輯
enforced（runbook §8.1 / §8.3）。

| `log_type`（stream class） | 對租戶可見？ | 理由 |
|---|---|---|
| `federation_audit` | ✅ **可策展**（tier-1）| 「平台關於該租戶」的營運事件（它的查詢／告警 eval／federation 行為），且帶可信 `tenant_id` 可歸屬 |
| `gateway_operational` | ⛔ platform-only | Envoy 操作層錯誤，非租戶可歸屬；落 `0:0` |
| `suspicious_audit` | ⛔ platform-only | audit-row 偽造偵測訊號（#566 T2-1），平台資安自用 |
| `prometheus_query_log` | ⛔ platform-only | 平台查詢成本資料（#552 chargeback），非租戶可見 |
| JWT-fail / 未帶有效 `tenant_id` 的列 | ⛔ platform-only | 無法歸屬到租戶 → fail-closed 落 `0:0`（runbook §8.3）|

> tier-1 的「只投影 `federation_audit` 且帶有效 `tenant_id`」由 Vector `tenant_route`
> 的 `abort`/`_unmatched` fail-closed 邏輯 enforced（runbook §8.3、`vector test` 的
> negative assertion）。本表是該行為的**可讀策展視圖**；要改 tier-1（讓另一個
> `log_type` 對租戶可見）＝改 Vector `tenant_route` 邏輯 + ADR review，**非**改本文件。

## Tier-2 — Tenant-visible field subset（租戶在自己分區裡看得到的欄位）

`federation_audit` 列被投影進租戶分區前，Vector **從零重建** event，只保留下列欄位
（fail-closed allowlist）。**SSOT = `helm/vector/values.yaml` `tenantProjectionKeepFields`**；
本表為其可讀對照，由 drift-guard 測試釘住一致。

| 欄位 | 內容 | 為何對租戶安全 |
|---|---|---|
| `tenant_id` | 租戶自己的 id | 分區本就是租戶自己的 |
| `log_type` | `federation_audit`（stream field）| 非基礎設施資訊 |
| `log_event_id` | ⛔ 跨分區 join key（回 `0:0` 完整副本）| 無語意的 UUIDv7；**永不移除**（移除＝斷值班 MTTR join 鏈，runbook §8.4）|
| `timestamp` | 事件時間（sink `_time_field`）| 非基礎設施資訊 |
| `status` | 租戶自己這次請求的結果碼 | 租戶自己的請求 |
| `method` | 租戶自己這次請求的 HTTP method | 租戶自己的請求 |
| `path` | 租戶自己這次請求的 path | 租戶自己的請求 |
| `query` | 租戶自己的 LogsQL query | 租戶自己的請求；長度由 `tenantProjectionMaxQueryBytes` cap（runbook §8.7）|
| `token_id` | 租戶自己的 federation token id | 租戶自己的 token |
| `duration_ms` | 租戶自己這次請求的延遲 | 租戶自己的請求 |
| `response_flags` | Envoy response flags | 非基礎設施識別資訊 |

**結構性排除（不在 allowlist ⇒ 租戶副本中不存在）**：`upstream`（後端 IP:port，原藏在 raw
`.message` 內）、`app` / `k8s_namespace`（基礎設施命名）、`pod_name` / `pod_ip` / `pod_node` /
`node_name`（拓樸）、payload 注入的偽造 `account_id`、以及任何**未來** gateway / producer
新增的欄位。這是 denylist→allowlist 反轉（對抗式 review）：allowlist **fail-closed**——
未明確 opt-in 的欄位一律不進租戶分區（runbook §8.3）。

### Stream fields（VictoriaLogs 索引維度）

租戶分區 sink 的 stream fields 是 tier-2 的**低基數**子集——SSOT =
`tenantProjectionStreamFields`（`tenant_id` / `log_type` / `status`）。⛔ **絕不**把
`query` / `token_id` / `path` / `log_event_id` 等高基數欄位設為 stream field（每個 distinct
值建一條 stream → RAM 爆，Gemini #894；runbook §8.7）；它們是 queryable data field，非 stream field。

## 租戶看不到（即使在自己分區）的東西 — 明確邊界

- **他租戶的任何列**：VictoriaLogs `AccountID` 分區硬隔離（gateway 注入已驗證 `AccountID`、
  client 自帶一律被 `replace()` 覆寫）。
- **平台拓樸欄位**：tier-2 allowlist 結構性排除（見上）。
- **平台 ops-only stream**（`gateway_operational` / `suspicious_audit` / `prometheus_query_log`
  / JWT-fail）：tier-1 不投影，永遠 `0:0`。
- **`(a)` 應用 log（`ProjectID=1`）**：Phase 2 defer-with-trigger（ADR-021 Future Work 1）；
  Phase 1 只有 `(b)` 平台營運 log（`ProjectID=0`）。

## 變更 SOP（誰改、改哪、怎麼驗）

| 要改什麼 | 改哪（enforced） | 同步 | 驗證 |
|---|---|---|---|
| 讓某欄位對租戶可見 / 不可見 | `helm/vector/values.yaml` `tenantProjectionKeepFields`（security-reviewed）| 本文件 §Tier-2 表 | `test_log_visibility_catalogue.py`（drift-guard）+ `test_vector_projection_vrl.py`（VRL allowlist）|
| 讓某 `log_type` 對租戶可見 / 不可見 | Vector `tenant_route` 邏輯 + ADR review | 本文件 §Tier-1 表 | `vector test`（runbook §8.6 negative assertion）|
| 改 stream field 維度 | `tenantProjectionStreamFields` | 本文件 §Stream fields | `test_vector_projection_vrl.py` |

> 所有變更走 GitOps commit 歷史（控制平面 audit；ADR-021 §Audit log control-plane）。

## 關聯

- [ADR-021 §MVP 範圍與可見度治理](../adr/021-tenant-log-query-federation.md) — 2-tier policy 的決策依據（安全 vs 治理邊界分離）
- [平台日誌彙整 runbook §8](platform-log-aggregation-runbook.md) — (b) 投影資料平面運作 + `vector test` 自證
- [租戶日誌查詢 onboarding 指南](../integration/tenant-log-query.md) — 租戶端視角（看得到哪些 stream / field）
- `helm/vector/values.yaml` `tenantProjectionKeepFields` / `tenantProjectionStreamFields` — tier-2 enforced SSOT
