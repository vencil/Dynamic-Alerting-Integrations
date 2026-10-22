---
title: "Tenant API Hardening (v2.8.0)"
date: 2026-04-29
audience: platform-ops, sre, security
verified-at-version: v2.8.0
---

# Tenant API Hardening — v2.8.0

> v2.7.0 出貨的 tenant-api 已具備基本 RBAC 與標準 chi 中介層（RequestID / RealIP / Logger / Recoverer / Timeout 30s）。本次 v2.8.0 是「客戶導入前的硬化」批次：補上**速率限制**、**X-Request-ID 回應標頭**、**Groups / Views / Task / PR 端點的租戶級授權**三個 production gap。
>
> 配套：middleware bundle + tenant-scoped authz 兩條軌道。

---

## 1. 速率限制（Rate Limiting）

### 1.1 規格

每位 caller 每滾動 60 秒視窗最多 N 個請求；逾限回 `429 Too Many Requests` + `Retry-After` 標頭 + JSON body：

```json
{
  "error": "rate limit exceeded for alice@example.com; try again in 42s",
  "code": "RATE_LIMITED",
  "retry_after_s": 42
}
```

### 1.2 配置

| Env 變數 | Helm value | 預設 | 說明 |
|---|---|---|---|
| `TA_RATE_LIMIT_PER_MIN` | （未來提供）| `100` | 每 caller 每分鐘請求上限 |

特殊值：
- `TA_RATE_LIMIT_PER_MIN=0` → 完全停用速率限制（單租戶 dev / CI runner 用）
- 未設 → fallback 預設 100（**不**算 malformed，`unset` 是合法狀態）
- 設為非數值或負數 → fallback 預設 100 **+ 啟動 log 印 `WARN: TA_RATE_LIMIT_PER_MIN=... is malformed ...`** — 防止操作者打錯字後預設 100 看似生效卻沒注意到 typo

### 1.3 Caller 識別優先序

按以下順序選 bucket key：

1. `X-Forwarded-Email`（oauth2-proxy 注入；production 主要識別）
2. `X-Real-IP`（unauthenticated 探針或 pre-auth 請求；以 source IP 限流）
3. `RemoteAddr` 的 IP 部分（最後 fallback）

> 與 `rbac.Middleware` 的 identity 來源完全一致 — 速率限制與授權層對「你是誰」永遠同步。

### 1.4 豁免路徑（不計入限制）

以下路徑**永遠**通過，避免 kube-probe 在每個 interval 燒掉 `system` caller 的 budget：

- `GET /health`
- `GET /ready`
- `GET /metrics`

### 1.5 設計選擇：homegrown 而非第三方套件

不引入 `httprate` / `golang.org/x/time/rate` 之類的依賴。homegrown 滑動視窗 ~80 行，`go.mod` 表面不擴張，邏輯本身**易於審計**：

- per-caller bucket = 單純的 `time.Time` slice（最舊在前，每次 write 修剪過期）
- 全局單一 `sync.Mutex` 保護 buckets map（throughput 限制遠大於網路 RTT，鎖競爭可忽略）
- 逾限回應計算 retry-after 用 caller-provided `now` 參數，不用 `time.Now()` — 確保 deterministic 測試（無需 sleep）

無界 caller bucket：每 caller 最多保留 `RequestsPerMinute` 個 timestamps + slice header。production identity universe 上限 ~數千，記憶體佔用無問題。若未來觀察到 anonymous IP flooding 之類的病態場景，可加 background sweeper goroutine — public middleware 介面不變。

---

## 2. X-Request-ID 回應標頭

### 2.1 為什麼需要

chi `middleware.RequestID` 會把 `X-Request-ID` 注入 request context（讓下游 handler + logger 用），但**不**回傳給呼叫者。客戶端因此無法把自己的 HTTP 請求對應回後端 log line — 影響 customer support 與 audit。

v2.8.0 起每個回應都會帶 `X-Request-ID` 標頭。

### 2.2 行為契約

| 場景 | 行為 |
|---|---|
| 請求**未**帶 `X-Request-ID` | chi 自動產生 UUID，注入 context，**回應**標頭也帶該值 |
| 請求**已**帶 `X-Request-ID`（客戶端 correlation ID）| chi 沿用該值；回應標頭原樣回傳（round-trip）|
| 請求 context 異常缺 RequestID | 回應**不**設標頭（防禦性，不會 crash）|

### 2.3 Customer 用法

```bash
# 客戶端產生 correlation ID 並 round-trip
curl -H "X-Request-ID: cust-incident-2026-04-29-001" \
     -H "Authorization: Bearer ..." \
     https://tenant-api.example.com/api/v1/tenants/db-a

# 回應 headers:
# HTTP/1.1 200 OK
# X-Request-ID: cust-incident-2026-04-29-001
# Content-Type: application/json
# ...
```

從此往後 grep 後端 log `cust-incident-2026-04-29-001` 即可定位該請求所有 audit lines。

### 2.4 伺服器內部錯誤只回固定訊息（[#1700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1700)）

`code` 為 `INTERNAL_ERROR` 的回應，`error` 一律是固定文字 `internal error; the server log has the details under this request_id`。原本的錯誤文字可能帶 conf.d 的絕對路徑、暫存檔路徑或 git 訊息，只寫進 server log，並附上同一個 `request_id`；排錯時拿回應裡的 `request_id` 去 grep log。少數本身就不含伺服器資訊、只說明哪個操作失敗的固定訊息（例如 conf.d 掃描失敗）照常回傳。

不經身分驗證的 `GET /ready` 也一樣（[#2811](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2811)）：body 只有 `status`，失敗時再加一個固定的 `reason`——`config_dir_unavailable`（config 目錄 stat 失敗）、`config_dir_not_a_directory`、`human_socket_down`（啟用 human socket 時自測失敗）；200 時只有 `{"status":"ready"}`。config 目錄、socket 路徑與錯誤原文只寫進 server log 的 `readiness check failed` 一行，附 `request_id`。status code 不變，kubelet 只看它。

tenant 寫入的其他錯誤也一樣：`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts` 與 batch 的逐筆結果，驗證錯誤與 merge 錯誤照常顯示原文，讀檔、暫存檔、git 這類伺服器端失敗改回 `the write failed; the server log has the details…`，HTTP status 不變。

兩個錯誤刻意保留文字，所以用自己的 code：

| code | status | 內容 |
|---|---|---|
| `CONFIG_DECODE_ERROR` | 500 | conf.d 設定內容解碼失敗：`/effective` 的 `parse defaults[i]: …`、custom-alerts 寫入時租戶檔內容無法 merge／解析、state store 的 `_groups.yaml`／`_views.yaml` 紀錄無法解析；文字說明哪個項目出錯，不含路徑 |
| `BASE_RESTORE_FAILED` | 500 | PR 模式寫入後 worktree 回不到 base：列出分支名與是否已推上 origin（不要重試，否則會再推一條分支）；不含底層 git 錯誤 |

### 2.5 路由與中介層的錯誤回應也改為 JSON 信封、由錯誤種類表決定（[#3226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3226)）

本節所列的出口，status、`code`、`Retry-After` 標頭與 `retry_after_s` 改由錯誤種類表決定（寫入路徑原本就是）；並非每個端點都已如此——仍有 handler 經 `WriteJSONError` 由 status 推出 `code`。以下原本不是 JSON 信封，或沒有 `code`，現在都是（`request_id` 照帶）：

| 情況 | 現在的回應 | 原本 |
|---|---|---|
| 沒有符合的路由 | 404 `NOT_FOUND` | 純文字 `404 page not found` |
| 路由存在但方法不對 | 405 `METHOD_NOT_ALLOWED`，`Allow` 標頭照舊列出可用方法 | 空 body 的 405 |
| 超過每個請求的期限（預設 30 秒）而 handler 沒回應 | 504 `REQUEST_TIMEOUT`，**不帶** `Retry-After`：handler 沒有回應，寫入是否生效不明，重送前先讀一次 | 空 body 的 504 |
| handler panic | 還沒回應時：500 `INTERNAL_ERROR`（固定文字，`request_id` 對得到帶 stack 的 log）；回應已開始寫出時：中斷連線，client 讀取會失敗，不會把截斷的回應當成完整 | 空 body 的 500；已開始寫出的回應照樣正常結束 |
| `GET /api/v1/events` 在關機中 | 503 `UPSTREAM_ERROR`，`Retry-After: 1`（重連到新的 pod） | 純文字 503 |
| `GET /api/v1/events` 的 writer 不能 flush | 500 `INTERNAL_ERROR`，文字 `streaming not supported` | 純文字 500 |
| logs federation 未設定（`POST /federation/tokens` 的 `logs` capability、`POST /federation/accounts/backfill`） | 503 `FEDERATION_NOT_CONFIGURED`，不帶 `Retry-After`：是部署設定，重試不會變 | 同（改由種類表決定） |

**批次的逐筆結果**（`results[].code`，含非同步 task 的結果）：`status` 為 `error` 的每一筆都帶 `code`，與同一失敗作為整個請求回應時的 `code` 相同；原本只有少數類別有，其餘是空字串。例如過載是 `WRITE_OVERLOADED`、conflict 是 `CONFLICT`、取消是 `UPSTREAM_ERROR`、結果未知是 `WRITE_OUTCOME_UNKNOWN`、驗證與 tenant id 錯誤是 `BAD_REQUEST`、沒有權限是 `FORBIDDEN`。同時：base 同步逾時（`FORGE_UNAVAILABLE`）不再當成內部錯誤；批次已 commit 但沒乾淨收尾時，逐筆文字改為「已 commit」那一句，不再是「conflict, retry」；多重包裝的錯誤依同一個順序判斷，逐筆結果與同步回應一致。

**租戶檔不是一般檔案**（FIFO、socket、device）：所有寫入入口一律 409 `TENANT_CONFIG_NOT_LOADABLE`，與 handler 提早拒絕同一個檔時相同（`config_error: not_regular_file`；單一租戶的寫入另帶 `tenant_id`，批次的整體回應不帶租戶、文字固定）；原本寫入途中才發現時，直寫回 400 `BAD_REQUEST`、PR 模式與批次回 500。

### 2.6 一個錯誤碼只有一種處理方式（[#3202](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3202)，⚠️ breaking）

錯誤回應的 `code` 現在等於一種處理方式：同一個 `code` 底下的所有失敗，client 該做的事都一樣，可以只看 `code` 決定下一步。原本有幾個 `code` 同時代表「重整後重送就會過」與「重送也不會過」，下表這些情況因此換成新的 `code`（status 與文字不變）：

| 情況 | 原本 | 現在 | 處理方式（`remedy`） |
|---|---|---|---|
| 兩個頂層檔宣告同一租戶（`<id>.yaml` 與 `<id>.yml`；`_federation/` 下兩個檔） | 409 `CONFLICT` | 409 `TENANT_FILE_AMBIGUOUS` | `fix_in_repo` |
| `/effective`：租戶被其他檔重複宣告（不是頂層的兩種拼法） | 409 `CONFLICT` | 409 `TENANT_DECLARED_ELSEWHERE` | `fix_in_repo` |
| 直寫批次已經 commit、但伺服器端沒乾淨收尾（`committed: true`） | 409 `CONFLICT` | 409 `BATCH_COMMITTED` | `refresh` |
| 租戶的 federation token 已達上限 | 409 `CONFLICT` | 409 `TOKEN_LIMIT_REACHED` | `existing` |
| 產生器這次沒判出結果、可重試（`busy`、`deadline`、`git_read`、`mismatch`、`no_time`） | 429 `GENERATOR_REJECTED` | 429 `GENERATOR_BUSY` | `retry_after` |
| 寫入結果未知（更新分支失敗且讀不回分支） | 503 `UPSTREAM_ERROR` | 503 `WRITE_OUTCOME_UNKNOWN` | `refresh` |
| 超過每個請求的期限、handler 沒回應 | 504 `UPSTREAM_ERROR` | 504 `REQUEST_TIMEOUT` | `refresh` |
| federation 未設定（logs federation；`--federation-prometheus-url` 未設時的 metric discovery） | 503 `UPSTREAM_ERROR` | 503 `FEDERATION_NOT_CONFIGURED` | `operator` |
| 路由存在但方法不對 | 405 `BAD_REQUEST` | 405 `METHOD_NOT_ALLOWED` | `fix_request` |
| PR 模式開 PR/MR 時，forge 拒絕 tenant-api 自己設定的 token | 403 `FORBIDDEN` | 403 `FORGE_FORBIDDEN` | `operator` |

拆碼之後，`CONFLICT` 只剩「重整後重送」（`base_hash` 不符、分支在批次執行期間被移動、state store 寫入搶輸），`UPSTREAM_ERROR` 只剩「稍後重送」（state store 或 forge 暫時失敗、伺服器關機中、寫入在 commit 前被取消），`FORBIDDEN` 只剩呼叫者自己的權限不足。由 status 推出 `code` 的回應不再把 409 標成 `CONFLICT`：每個 409 都帶自己的 `code`。

`remedy` 是封閉集合：

| `remedy` | client 該做的事 |
|---|---|
| `refresh_retry` | 請求所依據的狀態變了：重新讀取、重新套用變更後重送 |
| `retry_after` | 同一個請求稍後重送；回應帶 `Retry-After`／`retry_after_s` 時照它等 |
| `fix_request` | 請求本身要改（body、方法、大小、id） |
| `fix_in_repo` | 設定 repo 裡的內容要在 git 修好，經 tenant-api 的請求過不去 |
| `choose_other` | 要的名稱或 id 拿不到，換一個 |
| `permission` | 呼叫者缺身分或權限 |
| `operator` | 要由 tenant-api 的維運者修伺服器、儲存或部署設定，呼叫者無法處理 |
| `not_found` | 請求指名的東西不存在 |
| `existing` | 已經有東西擋著（已開的 PR/MR、租戶已持有的 token），處理那一個 |
| `refresh` | 寫入可能（或已經）生效：先重新讀取確認，不要直接重送 |

**單一來源**：所有 `code` 寫在 tenant-api 的 `internal/errcode`（handler、RBAC 中介層、產生器 client、非同步 task 都從這裡取，不再各自抄字串）。OpenAPI 的 `ErrorResponse.code` 因此成為列舉（`ErrorCode`）。契約檔 `tools/portal/src/interactive/tools/_common/data/tenant-api-errors.json`（portal 經 `_common/api/classify-error.js` 讀取，依碼的 `remedy` 決定顯示的訊息；缺少或不在契約內的碼當作未知，不套用任何處理方式的文案：有伺服器訊息就顯示它，沒有才顯示通用訊息）由 `make error-contract` 從同一份表、錯誤種類表，以及 handler／中介層直接寫出的回應（掃描原始碼取得；status 或 code 不是字面常數的寫法會讓產生器直接失敗。這道 fail-closed 只涵蓋經 `errcode` 常數、且經已列出的寫入函式寫出的回應：沒有 `code` 的回應（`http.Error`、`WriteHeader` 加 `Write`）與用字串字面量寫的 code 都不在契約內，portal 會把它們當作未知）產生，逐碼列出 `remedy`、`retryable`（`always`／`never`）、會出現的 status、預設的 `retry_after_s` 與對應的錯誤種類；`make error-contract-check`、pre-commit 與 CI 擋漂移。`SIMULATE_NOT_PROVIDED` 不在其中：那是 portal 部署自己的 nginx 501，不是 tenant-api 發出的。

### 2.7 錯誤回應的附加欄位由錯誤碼宣告（[#3272](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3272)）

錯誤回應除了固定欄位（`error`、`code`、`request_id`、`violations`、`retry_after_s`、`help`、`action`），有些碼會在同一層帶自己的欄位，例如 `PENDING_PR_EXISTS` 的 `existing_pr_url`、`pr_number`。每個碼可以帶哪些欄位、各是什麼 JSON 型別，宣告在 tenant-api 的 `internal/errcode`，每個欄位只宣告一次，所以同名欄位在每個碼底下都是同一個型別：

- **只送宣告過的欄位**：回應只帶該碼宣告過的欄位，其他的丟掉並寫錯誤 log。`INTERNAL_ERROR` 不宣告任何欄位，所以內部錯誤只回固定訊息與 `request_id`（§2.4）；宣告的欄位不能與固定欄位同名，所以蓋不掉 `code`、`error`。值為零的欄位照送（看不到的 PR/MR 是 `existing_pr_url: ""`、`pr_number: 0`；直接寫入批次的衝突是 `committed: false`；沒有可顯示的 finding 是 `findings: []`）。
- **契約與 OpenAPI**：`tenant-api-errors.json` 每個碼多一個 `extra`，列出欄位名、型別與陣列元素的型別。OpenAPI 的 `ErrorResponse` 列出所有碼的欄位聯集（Swagger 2.0 沒有 `oneOf`，一個回應只能有一個 schema），每個欄位的說明寫明哪些碼會帶它。
- **擋在出貨前**：`make error-contract`（`make api-docs` 在 swag 之後也會跑它）從型別檢查過的原始碼讀出每個 handler 設定的欄位與值的 Go 型別；設了該碼沒宣告的欄位、值的 JSON 型別與宣告不符、宣告了卻沒有 handler 設、宣告了固定欄位的名字，或 Extra 的 map 被傳進函式、複製、取址而讀不出最後有哪些鍵，產生器都直接失敗。pre-commit 與 CI 擋漂移。

宣告涵蓋現有的所有欄位，所以現有回應的內容不變。`GET /api/v1/tasks/{id}` 也補進了 OpenAPI。

---

## 3. Tenant-Scoped Authorization

v2.7.0 的 RBAC 透過 `rbacMgr.Middleware(perm, tenantIDFn)` 在路由層做 `PermRead`/`PermWrite` 檢查；對「path-param 內單一租戶」端點正確，但對**接收租戶清單**或**回應跨租戶資料**的端點留有 information disclosure 漏洞。v2.8.0 tenant-scoped authz 補完這四類端點的租戶級授權。

### 3.1 受影響端點 + 行為變更

| 端點 | v2.7.0 行為 | v2.8.0 行為 |
|---|---|---|
| `PUT /api/v1/groups/{id}` | 任何 `PermWrite` user 可編輯任意 group 的 `members` | 必須對請求帶來的**每個** member tenant 有 `PermWrite`；更新既有 group 時，也必須對它**既有的每個** member 有 `PermWrite`（[#1529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1529)：整份替換等於移除沒帶上的成員，與 DELETE 同一條規則）；缺者列入 403 訊息（列法見 §3.6）。`members` 至少一個（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)；清空群組請用 DELETE）；id 已被 caller 看不到的 group 佔用時回 409 `GROUP_ID_UNAVAILABLE`（見下方「group 可見性」）|
| `DELETE /api/v1/groups/{id}` | 任何 `PermWrite` user 可刪除任意 group | 必須對 group 既有**每個** member 有 `PermWrite`（防 DoS）；caller 看不到的 group 回 404（見下方「group 可見性」）|
| `POST /api/v1/groups/{id}/batch` | 回應列出 group 的全部成員 | `results[]` 只列 caller 可讀的成員，`summary` 也只算這些（[#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530)；同步、非同步、PR 模式皆同）；caller 看不到的 group 回 404 |
| `GET /api/v1/tasks/{id}` | 回傳完整 `Results[]`（含所有 task 觸及租戶）| 只有提交者看得到自己的 task；其他人一律拿到與「task 不存在」逐位元組相同的 404。task id 是 128-bit 隨機值。提交者看到的 `Results[]` 仍依其**當下**的讀取權限過濾，`summary` 依過濾後的結果重算（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）|
| `GET /api/v1/prs` | 回傳所有 pending PR/MR | bulk 模式：自動過濾不可讀租戶；`?tenant=<id>` 模式：不可讀回**空列表**（不 403，避免 existence oracle）。批次 PR/MR 只給讀得到它涵蓋的**每個**租戶的 caller；不知道涵蓋哪些租戶的（例如重啟前開的）只給讀得到全部租戶的 caller（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）|
| `GET /api/v1/events` | 每筆寫入都推給每個訂閱者 | 每個訂閱者只收到它讀得到的租戶的事件，事件送出當下以最新的 RBAC 判斷；不屬於單一租戶的 state 紀錄（groups、federation policy、AccountID 帳本）只送給讀得到全部租戶的 caller（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）；views 的事件送給讀得到 saved views 的 caller，與 `GET /views` 同一道門檻（見 §3.3，[#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)）|
| `DELETE /api/v1/federation/tokens/{id}` | 無權撤銷時回 403，訊息帶 token 所屬租戶 | 無權撤銷時回與「token 不存在」逐位元組相同的 404（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）|
| `PUT /api/v1/tenants/{id}` | 只要求對 `{id}` 有寫入權限，body 整檔寫入 `{id}` 所在的檔；同檔其他租戶的段落可被刪掉或改寫 | 只寫「API 管理」的租戶：`{id}` 的檔只屬於它自己。共用檔等其他形狀的租戶改由 git 管理，所有寫入一律 409 `TENANT_GIT_MANAGED`（**breaking**，[#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)，見下方「API 管理與 git 管理」）|

**group 可見性**：`GET /groups`、`GET /groups/{id}`、`DELETE /groups/{id}`、`POST /groups/{id}/batch` 共用同一個判斷——caller 至少能讀一個成員，才算看得到。看不到的 group 一律回與「不存在」逐位元組相同的 404，而且在讀 body 之前就回，所以任何一個端點都不會證實它存在。沒有成員的 group（`PUT` 已不能建立，只會是先前留下的）只有規則對 org、environments、domains 都不設限的平台管理員看得到、刪得掉；open mode 照舊全部看得到（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）。`PUT` 是 upsert：id 已被 caller 看不到的 group 佔用時，回固定內容的 409 `GROUP_ID_UNAVAILABLE`，不論那個 group 有哪些成員都逐位元組相同（不可重試，請換 id；與狀態寫入搶輸時的 409 `CONFLICT` 不同）。⚠️ 已接受的揭露：這仍透露「這個 id 已被使用」一個位元——group id 是所有 caller 共用的命名空間，Kubernetes 物件名稱、S3 bucket 名稱也是同樣的取捨；它不再透露該 group 的任何內容。⚠️ 第二個已接受的揭露：「這個 group 有你不能寫的成員」。`PUT`／`DELETE /groups/{id}` 遇到不能寫的既有成員時，403 訊息會說「it also has member tenants you cannot view」（見 §3.6，[#1529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1529)）；`POST /groups/{id}/batch` 對一個看得到的 group 執行時，只要有任何成員被拒（包括 caller 讀不到、也寫不了的成員）就整批不寫入、回 422，`results[]` 只列 caller 讀得到的成員——被拒的標 `error`，其餘都是 `not_applied`（[#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530)）。所以看得到的成員全是 `not_applied` 的 422，就表示被拒的是一個看不到的成員。兩者透露的是同一個位元，不透露是哪個成員、有幾個，也不透露成員的任何內容。[#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520) 之後只能讀單一租戶或前綴的 caller 也到得了這些端點，所以這個位元現在對他們也成立；它是群組跨越 caller 權限邊界時必然的結果——不揭露的做法是讓批次只寫 caller 能寫的成員、默默略過其他，而群組批次是全有或全無（#2900），不採用。

**逐租戶判斷用的是哪個述詞，擋得住什麼**（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）：上表 `/events`、`/prs`、`/tasks/{id}` 的結果過濾，與 group 可見性，用的都是讀取平面所有列表過濾共用的同一個述詞（`OrgAllowedRead`）。它只判斷兩件事：規則的 `tenants` 是否涵蓋該租戶，以及 org 軸。org 軸上，對標了別的 org 的租戶，org-scoped 呼叫者在 shadow 與 enforce 下都被濾掉；沒標 org 的租戶要開 `--rbac-org-scope-enforce` 才會被濾掉，shadow 下照樣看得到。⚠️ `environments`／`domains` **不是**讀取平面的邊界：這個述詞不看它們，只限制了 environment 或 domain 的 `tenants: ["*"]` 呼叫者，在這些端點上照樣看得到每個租戶的項目。「讀得到全部租戶」（不屬於單一租戶的事件、租戶不明的批次 PR）的門檻則是規則對 `"*"` 有讀取權、且不帶 org-scope、environments、domains，所以上述呼叫者收不到這類項目。

**API 管理與 git 管理**（[#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)，breaking；取代 [#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062) 的段落拼接與 [#3050](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3050) 對租戶檔的隱藏）：RBAC 以租戶 id 授權，而寫入、`source_hash` 與原文都以檔案為單位。兩者只有在檔案只屬於一個租戶時才一致，所以 tenant-api 以檔案形狀劃線。

- **API 管理**：租戶的檔（照舊依檔名 stem 找到）是「API 形狀」——只有一份 YAML document（之後只能是空 document；這一條只決定「誰來改這個檔」，空的尾端 document 本身仍讓 `parse-tree` 判為讀不了，見本段末的 body 規則與 config_error）；頂層是 mapping；頂層與 `tenants:` 底下的 key 都是不帶 tag 的純量（plain 或加引號；`!!str` 也算帶 tag），沒有 merge key（`<<`），key 與值都不帶 anchor、值不是 alias；`tenants:` 只宣告一個租戶，且解碼後的 id 等於檔名。租戶自己段落**裡面**的 anchor／alias 不受限，整份檔都是它自己的。還沒宣告任何租戶的檔（空檔、只有註解、`tenants: {}`）也算，寫入會填滿它。判定只有一份（`internal/confd/managed.go` 的 `TenantFileManagement`），id 以 threshold-exporter 解碼器讀到的為準（`cfg.ParseTenantFile` 的租戶集合也一併比對），所以 `? !!binary ZGItYQ==`（解碼後是 `db-a`）這種寫法不會被當成 API 形狀。這類租戶照舊：`GET` 回原文、`source_hash`、`custom_alerts`，`managed_by: "api"`；`source_hash` 天生只涵蓋這一個租戶，`X-DA-Base-Hash` 與 custom-alerts 的 `base_hash` 照常使用。`PUT` 的 body 必須同樣是 API 形狀（只宣告 `{id}`），否則 400，所以 API 寫入不會把檔變成 git 管理。body 也必須是 `da-guard parse-tree` 讀得了的（這是第二道條件，與 API 形狀分開判：只有一份文件，連只有 `---` 的尾端文件也不行；不帶 `!!binary`、`!!set`、`!foo`、`!` 這類白名單外的 tag），否則 400，錯誤文字寫出被拒的 `kind` 與行號（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）。
- **git 管理**：其他所有形狀——多租戶共用的檔、檔名與宣告的 id 不同、多份 document、頂層或 `tenants:` 底下有 anchor／alias／merge key／帶 tag 的 key、不是 YAML。`GET` 只回 effective 結果（`resolved_thresholds`、`validation_warnings`／`validation_notices`，或 `config_error`），**不回** `raw_yaml`、`source_hash`、`custom_alerts`，並帶 `managed_by: "git"` 與 `git_managed_reason`（`not_tenant_document`、`several_documents`、`yaml_indirection`、`tenant_count`、`id_mismatch`，固定字串，不點名其他租戶）；對所有呼叫者都一樣，讀得到檔內全部租戶的也一樣。所有寫入路徑——`PUT`（兩種寫入模式）、`PUT .../custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`（同步、非同步、PR 模式）——一律拒收，回 409 `TENANT_GIT_MANAGED`，信封帶 `tenant_id` 與 `git_managed_reason`，訊息不點名其他租戶，不寫入；批次則是該筆結果的 code。`/diff` 回同一個 409（只能讀 `{id}` 的呼叫者也是）；`/validate` 回 `valid: false`、`warnings` 是同一段文字，另帶 `code: "TENANT_GIT_MANAGED"`。要改這類租戶請直接改 git。
- **列表也標出來**（[#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157)）：`GET /tenants` 與 `/tenants/search` 的每一列帶 `managed_by`，`git` 時另帶 `git_managed_reason`，與同一份檔的 `GET /tenants/{id}` 判定、代碼完全相同（同一個述詞）。讀不到檔的降級列（`config_error` 為 `unreadable`、`not_regular_file`）不帶這兩個欄位。檔名與檔內唯一宣告的 id 不同時，這一列照舊以檔名為 id，帶 `managed_by: "git"`、`git_managed_reason: "id_mismatch"`，其他欄位全空——檔內的內容屬於宣告的那個 id，列表不顯示它，也不回宣告的 id。檔內的 metadata 屬於別的 id，這一列自己的 metadata 是**未知**、不是「未標記」，所以標 `metadata_incomplete: true`，可見性比照降級列。其他 git 管理的列，只要檔內解碼出的租戶不含這一列的 id（例如共用檔只宣告了別的 id、或這個 id 只出現在第二份 document），同樣這樣處理；檔內有宣告這個 id 的，照常讀它自己的 metadata。⚠️ **可見性變更（breaking）**：先前這種列被當成「未標記」，shadow 模式下限制 `environments`／`domains` 的呼叫者看得到；現在 shadow 與 enforce 模式下都看不到，只有規則對兩軸都不設限的呼叫者看得到。
- **為什麼是 409**：請求本身格式正確，對 API 管理的租戶會被接受；拒收是因為目標檔目前的狀態，與 `TENANT_CONFIG_NOT_LOADABLE` 同一類。跟 `CONFLICT`（base hash 不符，重整重試即可）不同，重試不會成功，所以另立一個 code。
- **判定的時機**：寫入在寫入鎖內、在要被取代的那份檔上判（PR 模式是切分支的最新 base 上那份），所以排隊期間檔被改成共用也會被拒；判定在 `X-DA-Base-Hash` 之前，所以猜中共用檔的 hash 也只會拿到同一個 409，不能用 `CONFLICT` 與否確認猜測。直接模式的 409 `CONFLICT` 只在現在的檔仍是 API 形狀時附 `current_source_hash`。PR 模式的批次 pre-flight 讀本機的樹，本機判為 git 管理時不擋，交給最新 base 判；PR 模式的 `/validate` 照舊只跑 body 本身的檢查（#1718），不判 git 管理。
- **custom-alerts 在鎖外合併為什麼安全**：handler 在寫入鎖外讀檔、合併 recipe，再以 `WriteIfUnchanged` 帶 `base_hash` 寫回整份檔。被合併的檔已確認是 API 形狀，整份只有這個租戶；鎖內的 hash 比對成立，代表寫回的就是那份檔只改了 `_custom_alerts`，不會蓋掉別的租戶的內容。鎖內若發現檔已變成 git 管理，回 409 `TENANT_GIT_MANAGED`。
- **`/validate` 與 `/diff` 與 `PUT` 走同一套拒收判定**（[#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)）：依 `PUT` 的順序——`{id}` 的寫入範圍（依檔內現況與 body 提議的 metadata）、遮罩後的憑證 placeholder，接著 body 本身的檢查（含 API 形狀），再在要被取代的檔上：解析租戶檔（含重複宣告）、git 管理、驗證。`PUT` 會拒的，`/diff` 回同一個狀態碼與 body；`/validate` 回 `valid: false` 與同一段文字。例外：讀得到 `{id}` 但不能寫的呼叫者，`/diff` 給遮罩預覽而不是 `PUT` 的 403（#1560）；PR 模式下 `PUT` 的後段在最新 base 上判，`/validate` 只跑 body 本身的檢查，`/diff` 以伺服器的樹預覽。
- **`GET` 帶出「別處宣告」的旗標**（[#3172](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3172)）：`GET /tenants/{id}` 的每種 200 回應都帶 `declared_elsewhere`：`true` 表示伺服器目前讀到的樹裡，除了 `{id}` 自己的檔，還有別的檔宣告了 `{id}`——也就是寫入守衛回 409 `TENANT_DECLARED_ELSEWHERE` 的條件，判法與 `/diff` 相同（同一支函式、同一份共用的讀取 walk）。它不保證寫入的回應：寫入可能先因別的原因被拒（寫入範圍、body、租戶檔不是一般檔），PR 模式的寫入則在最新 base 上判，那棵樹可能與這裡讀到的不同。`true` 涵蓋兩種情況：**重複宣告**（自己的檔也宣告 `{id}`）——threshold-exporter 拒收整棵樹的重載、停在上一份好的設定，`/effective` 回 409；**只宣告在別處**（自己的檔沒宣告 `{id}`，例如 `tenants: {}`）——exporter 照常從別的檔載入，`/effective` 回 200，但寫入守衛一樣不讓寫進自己的檔。它只是一個布林，不點名是哪個檔、也不點名那個檔裡的其他租戶。**不增加揭露面**：重複宣告的情況早已由 `/effective` 以同樣的讀取權限公開（409，固定文字），兩種情況 `/diff` 對只能讀 `{id}` 的呼叫者也可能回同一個 409。conf.d walk 跑不了（逾時、目錄讀不到）時欄位**不出現**，不是 `false`；原因只寫進 server log。
- **相對 #3062 的行為改變**：共用檔裡的租戶不能再經 API 寫入（先前 body 宣告的段落會拼進共用檔）；不是 YAML 的檔也是 git 管理，對所有租戶都有寫入權限的人也不能再以 `PUT` 整檔取代（#2405 的權限規則只剩 API 形狀、但結束支援檢查讀不懂 `tenants:` 的檔）；頂層 key 帶 `!!str` tag 的 body 現在回 400。
- **相對先前版本的行為改變：只能讀 `{id}` 的呼叫者打 `/validate`**：`/validate` 的路由只要求讀取權限，先前它不判寫入範圍，這類呼叫者拿得到「body 本身是否有效」的判斷；現在三個端點同判，`{id}` 的寫入範圍不過即 `valid: false`、`warnings` 是 `{id}` 寫入閘門的固定文字，與他的 `PUT` 會被拒一致。只想檢查 body 寫法的讀者，請改用 `/diff`（遮罩預覽，body 驗證錯誤照 `PUT` 回 400）。

⚠️ 這張表描述的是 handler 層。URL 不帶租戶 id 的端點在路由層還有一道閘門（[#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)）：呼叫者要**至少能讀一個租戶**（寫入端點：至少能寫一個）——有一條命中他的規則授予該權限，且 `tenants` 至少列了一個合法的 pattern。規則帶 `org-scope` 而呼叫者沒有那個 claim 時，只在 org 軸 shadow 下算數：開了 `--rbac-org-scope-enforce`，這種規則對任何租戶都授不了權。

這個判斷**只看呼叫者自己的規則與 org enforce 旗標**，不看 conf.d 或 `_tenant_orgs.yaml`。所以規則列了一個不存在的租戶也過得了閘門，拿到的是空列表；閘門回 200 還是 403 不透露任何租戶存不存在、屬於哪個 org。過了閘門看得到什麼，全由上表的 handler 過濾決定。沒有規則命中，或 `_rbac.yaml` 有設定卻是零條規則（MED-8），照舊 403，訊息是 `insufficient permissions: no tenant grants read`（寫入端點為 `write`），不帶租戶 id。

`GET /me` 不在這道閘門後：它只回答「我是誰、我的規則怎麼寫」（同 OIDC userinfo、GitHub `GET /user`），所以只要求身分——缺 `X-Forwarded-Email` 回 401，不要求任何授權。沒有規則命中、規則因 org 軸 enforce 而授不了權，或 `_rbac.yaml` 是零條規則（MED-8）的呼叫者都拿到 200：`permissions` 只列命中他的規則（可能是空的 `{}`，也可能是此刻授不了權的規則），`accessible_tenants` 只列這些規則自己寫的 pattern（可能是 `[]`）。它不讀任何租戶資料，也不列別人的規則，所以 MED-8 的 fail-closed 不受影響——那條管的是資料存取，而「你沒有任何授權」本來就是呼叫者自己該知道的事。

相對先前版本的行為改變：先前這道閘門要求對 `*` 有權限，只有規則寫了 `tenants: ["*"]` 的呼叫者過得了；單租戶或前綴授權（如 `["db-a-*"]`）的呼叫者在 `/me`、`/tenants`、`/tenants/search`、`/groups`、`/tasks/{id}`、`/prs`、`/events`、`/federation/tokens`、`POST /tenants/batch` 一律 403。現在這些呼叫者拿得到自己租戶的內容；`/federation/tokens`、`POST /tenants/batch`、`POST /groups/{id}/batch`、`PUT`／`DELETE /groups/{id}` 照舊由 handler 逐租戶判寫入或管理權限。`GET /federation/policy`（平台的指標白名單，不點名任何租戶）也開放給至少能讀一個租戶的呼叫者，租戶管理員挑自己的子集時需要它。反方向收緊的只有一種情況：org-scoped 的 `tenants: ["*"]` 規則、呼叫者沒帶 org claim、且 org 軸是 enforce——它先前過得了閘門、拿到空列表，現在是 403。

仍要求對 `*` 有權限的路由：saved views 的四個端點（見 §3.3）、`PUT /federation/policy`、`POST /federation/accounts/backfill`，以及 `/audit/...`。後三者 handler 的門檻本來就是平台管理員，換成較寬的閘門不會多放任何人進來。這裡的「平台管理員」指 `tenants: ["*"]`、帶 `admin`、**且不帶 `org-scope`** 的規則：帶 org-scope 的 `"*"` admin 規則過得了路由閘門，但在這三者的 handler 一律 403，不論 org-scope 是否 enforce（[#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)）。org-scope 把規則限縮在某些組織的租戶，而改全平台設定、對全艦隊配發 `account_id` 不屬於任何一個組織。

### 3.2 為什麼 `?tenant=<id>` 不直接回 403

對 `GET /api/v1/prs?tenant=db-secret` 這類 query，403 會**洩露 db-secret 的存在性**（caller 能看到「我的權限不夠」=「該租戶確實存在」）。空列表則和「該租戶沒有 pending PR」無從區分 — 是 API surface 想要的行為。

> **乍看是 bug-feature，實際是刻意 security UX choice。** 後續重構 PR 若把它「修」成 403，會回退這個 oracle，因此 `TestListPRs_TenantQueryReturnsEmptyWhenForbidden` 鎖死當前行為。

### 3.3 為什麼 Views 不在範圍內

`PutView` / `DeleteView` 也接收 `Filters map[string]string`，看起來像 group 的 members。但 view filters 是**任意 metadata 字串**（例 `severity:critical`、`team:platform`），不是嚴格 tenant ID 列表 — 不能對 filter 內容做 RBAC 檢查（不知道該檢查什麼）。view 真正暴露 tenant 資料的時機是 dashboard 用 view 跑 query，那時 tenant 級 RBAC **本來就**會檢查。

→ Views 留待未來如果加「filter 必須為 tenant ID 列表」型別約束時再補。

同理，views 沒有擁有者，handler 也不按租戶過濾，所以四個 view 端點的路由閘門仍要求對 `*` 有權限（[#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)）；否則任何讀得到一個租戶的人都看得到、改得到每個 view。

views 也不屬於任何組織，所以 handler 另外要求那條 `"*"` 規則**不帶 `org-scope`**（[#3160](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3160)）：`GET /views`、`GET /views/{id}` 要讀取權限，`PUT`／`DELETE /views/{id}` 要寫入權限，不看 environments／domains。帶 org-scope 的 `"*"` 規則過得了路由閘門，但這四個端點一律 403，不論 org-scope 是否 enforce。open mode（沒有 `_rbac.yaml`）照舊讀得到、寫不了；設定了 `_rbac.yaml` 但零條規則時照舊全部 403。views 改變的事件（`GET /events`）送給同一批讀得到 views 的 caller。portal 讀 views 拿到 403 時隱藏 saved views 面板，不跳錯誤。依 org 分區的 views 不在本次範圍，有 org-scoped 使用者需要時再議。

### 3.4 為什麼 `_metadata` 路徑推斷不在範圍內

ADR-016 提到「flat tenant 缺 `_metadata.{domain,region,environment}` 時可從父目錄路徑推斷」— 這是**遷移工具**（`migrate_conf_d.py`）的功能，runtime tenant-api 不做。本次硬化不擴 RBAC core 行為。

### 3.5 Open-mode RBAC（缺 `_rbac.yaml`）行為

| Permission | Open-mode 行為 |
|---|---|
| `PermRead` | **全 grant**（pre-prod / dev 用，所有 authenticated user 可讀）|
| `PermWrite` | **不 grant**（避免無 RBAC 配置的環境誤允許寫）|
| `PermAdmin` | **不 grant** |

→ Open-mode 環境下，`PutGroup` / `DeleteGroup` 仍會在新 tenant-scoped check 處被擋下（因 PermWrite 拒）。這是預期行為：production hardening 不該因為操作者忘了部署 `_rbac.yaml` 而退化為「人人可寫」。

### 3.6 錯誤訊息設計

403 訊息**完整列出**所有禁用的 tenant ID — 不只第一個。理由：操作者調修權限時**一次知道所有需要修的**比 retry-and-discover 高效。

```json
{
  "error": "insufficient permission to write group with forbidden member tenants: db-b, db-c"
}
```

去重 + 按請求順序保留：caller 可直接 grep 自己的 RBAC config 找原因。

**列出哪些 id 取決於 id 從哪來（[#1531](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1531)）**：請求帶來的 id 一律列出（caller 本來就知道）；從磁碟上 group 讀出的既有成員，只列 caller **讀得到**的，讀不到的只加一句不帶數字的說明：

```json
{
  "error": "insufficient permission to delete group with forbidden member tenants: db-b; it also has member tenants you cannot view"
}
```

全部都讀不到時是 `insufficient permission to delete group: it has member tenants you cannot view`。不給數字：403 本身已表示有東西被擋，數字則會透露讀取端藏起來的成員有幾個。

### 3.7 receiver 憑證只給有寫入權限的人看（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）

行為變更：原本 `GET /tenants/{id}`、`/effective`、`POST /tenants/{id}/diff` 把租戶檔裡的 receiver 憑證原樣回給任何讀得到該租戶的人；出貨的 `viewers` 規則是 `tenants: ["*"]` 讀取，所以等於所有 viewer 都讀得到所有租戶的 webhook URL。現在只有**對該租戶有寫入權限**的呼叫者看到原值，其他人看到 `<masked: write permission required>`，回應帶 `masked: true`。

**誰看得到**：判定與 `PUT /tenants/{id}` 的閘門用同一個述詞（org 軸、`environments`／`domains` 軸都一樣）。所以被遮罩的人一定不能 `PUT`，不存在「讀遮罩版、改一行、寫回去」把佔位字串蓋掉真值的路徑。open mode（沒有 `_rbac.yaml`）不授予寫入權限，所以所有人都看到遮罩版。

**遮哪些**：依 key 名比對，不限位置、不分大小寫。整個值換成佔位字串的容器：`http_config`（route generator 原樣交給 Alertmanager，`proxy_connect_header`、`http_headers`、`tls_config` 的 key、`oauth2.endpoint_params` 都可能放祕密，欄位清單追不上）、`tls_config`、`headers`、`http_headers`、`proxy_connect_header`、`endpoint_params`；單一欄位：`url`、`api_url`、`webhook_url`、`proxy_url`、`routing_key`、`service_key`、`auth_password`、`auth_secret`、`password`、`bearer_token`、`credentials`、`client_secret`、`api_key`、`token`、`bot_token`、`user_key`。receiver 至少出現在五個位置（`_routing.receiver`、`overrides[i].receiver`、`routes[i].receiver`、`_routing_defaults`、routing profile），照路徑列舉的清單會在第六個位置出現時漏掉；照 key 名比對的代價是同名但不是憑證的值也會被遮。名單不由 schema 推導，但 `tenant-config.schema.json` 標了 `"x-credential": true` 的欄位名一律得在名單內，`tests/ops/test_lib_redact.py` 守。遮罩名單沒有 `*_file`／`*_ref` 欄位，因為寫入時就擋掉了：receiver 任何一層帶這類鍵（或 `files`）的寫入，路由產生器整棵樹拒收，`PUT` 回 422 `GENERATOR_FINDINGS`（[#3257](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3257)）。

| 出口 | 被遮罩的人看到的 |
|---|---|
| `GET /tenants/{id}` | `raw_yaml` 是重新序列化、**不含註解**的版本（註解掉的舊 URL 也是憑證）；`custom_alerts` 同樣遮罩。多行的值（block scalar）收成一行佔位字串 |
| `GET /tenants/{id}/effective` | `effective_config` 每一層（defaults 鏈、平台檔、profile、租戶檔）的憑證都遮；解碼失敗的 500 `CONFIG_DECODE_ERROR` 改回固定文字（yaml 型別錯誤會引用出錯的值） |
| `POST /tenants/{id}/diff` | 先遮罩**兩側**再比對與產生 diff。只改憑證的提案回 `has_diff: false`。原本的實作先比對原始位元組，猜對整份檔就回空 diff、猜錯就有 diff，等於可以逐個猜憑證；遮罩後兩側相同，這條路就不存在 |

**無法確定遮乾淨時一律不給**：檔案用了 anchor／alias／merge key（`_metadata.owner: &h <secret>` 再 `api_url: *h`，憑證就落在不叫憑證的 key 上）、有多個 document、或不是 YAML。`GET` 回空的 `raw_yaml` 與 `custom_alerts` 加 `raw_yaml_withheld: true`；`/diff` 回 422 `MASKED_PREVIEW_UNAVAILABLE`。被遮罩的人送的提案超過租戶文件大小上限或 request body 上限時回 413，不截斷後照算。

**寫回保護**：`PUT /tenants/{id}` 的 body 只要有任何值**解碼後**等於佔位字串就回 400，引號、`!!str`、block scalar、alias 都一樣；`POST /tenants/{id}/validate` 對同一個 body 回 200 `valid: false` 並列出同一條理由。

**git 管理的租戶不給原文**（[#3064](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3064)，取代 #3050 對租戶檔的隱藏）：上面每個出口讀的都是**整份檔**（原文、從中解碼的值或它的 hash，各出口的回應形狀見上表），而 `GET`、`/diff` 與所有寫入只處理 API 形狀、只屬於該租戶的檔（見 §3.1「API 管理與 git 管理」），其他形狀的租戶 `GET` 只回 effective 結果，不回 `raw_yaml`、`source_hash`、`custom_alerts`。`/effective` 的 `source_hash` 與 `GET` 一致：只在租戶是 API 管理時給，git 管理的租戶對任何呼叫者都是空字串並帶 `source_hash_withheld: true`（寫入一律被拒，這個 hash 沒有用途，又能離線驗證對整份檔的猜測）。`/effective` 仍保留 #3050 對解碼錯誤的守衛：錯誤可能來自合法宣告多個租戶的根層平台檔（`_defaults.yaml` 鏈），所以檔內有呼叫者讀不到的租戶（或列不出有哪些租戶，且呼叫者讀不到全部租戶）時，解碼失敗的 500 改回固定文字。

**hash 也不給**：被遮罩的人拿到的 `source_hash`（`GET`、`/effective`）與 `merged_hash`（`/effective`）是空字串。`merged_hash` 是解碼後合併結果的 hash，被遮罩的人手上有其他所有值，把猜測填回佔位字串的位置、用公開的演算法算一次就能離線驗證猜對沒有，低熵的密碼可以字典攻擊；`source_hash` 是原檔位元組的 hash，同樣能驗證對整份檔的猜測。被遮罩的人不能寫入，也用不到 `source_hash` 當 `base_hash`。

**檔案 key 帶 tag 時不給**：key 寫成 `? !!binary YXBpX3VybA==` 會解碼成 `api_url`，照字面比對看不出來，所以 key 帶 `!!str` 以外的明確 tag 也算無法確定遮乾淨。

這是過渡方案，長期解是讓憑證改用參照形態（Secret 參照，寫入後不再回傳；#1560 選項 c）。

### 3.8 conf.d 有讀不了的項目時，寫入整個拒收（[#2939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2939)）

**行為變更（breaking）**：寫入判的是「寫入後的整棵 conf.d」（[ADR-036 §4](../adr/036-single-parser-effective-config.md)）。route generator 的 `--validate --strict` 遇到任何讀不了的項目就拒收整棵樹，`da-guard` 回 exit 3；tenant-api 的寫入因此一併拒收，什麼都不寫。回應依「能不能用 git 修」分成兩種：

| 項目（walker 記錄的 `reason`） | 能不能用 git 修 | 回應 |
|---|---|---|
| 無法解析的 symlink（`stat_error`：link 在，但跟不下去——指向的目標不在、迴圈（含指向自己）、路徑中段不是目錄） | 能：它沒有內容，git 以 mode 120000 記錄它 | 409 `TENANT_TREE_UNREADABLE` |
| 設定檔名的 FIFO、socket、裝置檔，或指向它們的 symlink（`special_file`） | 不能：git 記錄不了 | 500 `TENANT_STORE_FAULT` |
| stat 成功但讀取失敗，例如 I/O 錯誤（`read_error`） | 不能：是伺服器端的儲存狀態 | 500 `TENANT_STORE_FAULT` |
| 列不出內容的子目錄（`walk_error`） | 不能 | 500 `TENANT_STORE_FAULT` |
| 其他 stat 失敗，例如權限不足（`stat_error`，但不是上面那種） | 不能：伺服器的權限，git 修不了 | 500 `TENANT_STORE_FAULT` |

兩種同時存在時回 `TENANT_STORE_FAULT`：伺服器那份 conf.d 得先修好，關於設定內容的回答才有意義；回 409 會把用戶端引去 git 修一個 git 修不了的問題。`TENANT_STORE_FAULT` 是 tenant-api 自己那份 conf.d 的儲存問題，不是設定內容，也不是 [ADR-036 §5](../adr/036-single-parser-effective-config.md) 的 503（產生器容器不可用）。

| 端點 | 無法解析的 symlink | 伺服器端讀不了 |
|---|---|---|
| `PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts` | 409 `TENANT_TREE_UNREADABLE` | 500 `TENANT_STORE_FAULT` |
| `POST /tenants/{id}/diff` | 409（與它預覽的 PUT 相同） | 500（同） |
| `POST /tenants/{id}/validate` | 200、`valid: false`，`warnings` 是 PUT 會回的同一段文字 | 同左 |
| `POST /tenants/batch`、`POST /groups/{id}/batch`（直寫與 PR 模式） | 422，整批不寫；每一筆沒有其他拒收原因的 op 都是 `status: error`、`code: TENANT_TREE_UNREADABLE` | **整批**回 500 `TENANT_STORE_FAULT`（不是 422 逐筆：問題不屬於任何一筆，改哪一筆都過不去，與佇列滿、worktree 停在 PR 分支等整批失敗同一形狀）；非同步批次 task 為 `failed`，每筆結果帶同一個 code |

**PR 模式的 `/validate` 例外**：PR 模式的 validate 只做 body 檢查、不讀樹（#1718：本機的樹可能落後 base），所以不會預告這兩種拒收，要到切分支時才判。

- **要寫的租戶檔本身**：
  - 是無法解析的 symlink（懸空、迴圈含自我迴圈、ENOTDIR）：寫入把它換成一般檔（直寫與 PR 模式都以 rename 取代目錄項目本身，不會順著 symlink 寫到它指向的地方），照常放行。這是 ADR-036 §4 的修復路徑：它沒有內容，換掉不會損失任何東西。批次扣掉的是整批所有 op 的租戶檔。直寫模式下這個 symlink 必須已 commit：沒 commit 的 symlink 是工作樹與 HEAD 不一致，回 409 `TENANT_FILE_DIRTY`（見 §3.10）。
  - 讀取失敗（`read_error`）、是 FIFO 等非一般檔，或其他 stat 失敗：一律回 409 `TENANT_CONFIG_NOT_LOADABLE`（`config_error` 為 `unreadable` 或 `not_regular_file`；批次該筆同 code），**不分角色**，對所有租戶都有寫入權限的人也一樣。這個檔有內容只是讀不到，整檔替換等於靜默抹掉內容（#2405 原本就規定讀不了一律拒收）。它要在伺服器或 volume 上修，或從 git 重設。
- **以前的回應**：別處有 FIFO 時，單筆寫入卡住約 5 秒（掃描時限）後回 500，批次在複製 conf.d 時回 500；懸空的 symlink 等其他項目不擋寫入。
- **PR 模式**判的是切出分支的最新 base，也就是 git 裡的樹：PR 合併後服務吃的就是這份內容。服務中目錄裡沒 commit 的項目（未追蹤或被 `.gitignore` 的 FIFO 之類）不在那裡，所以不擋 PR 模式的寫入；單筆 PR 寫入與 PR 批次判的是同一棵樹，結果一致（[#2972](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2972)；以前單筆 PR 寫入判的是服務中的工作目錄，會因這類項目被拒）。這類項目仍會擋直寫模式，也照樣計入 `tenant_api_store_unreadable_entries`。例外：要寫的租戶檔本身在服務目錄裡是未追蹤的非一般檔（FIFO、socket、裝置檔、目錄，或指向它們的 symlink）時，PR 寫入回 409 `TENANT_CONFIG_NOT_LOADABLE`，與直寫模式同一個 code，不覆蓋它：PR 寫入會在服務目錄裡改寫這個路徑，覆蓋後再切回 base，這個項目就被刪掉了。
- **誰看得到檔名**：讀不了的項目可能是任何租戶的檔，所以只有對**所有租戶**有寫入權限的呼叫者（RBAC 規則 `tenants: ["*"]`、帶 `write`，且沒有 org-scope、environments、domains；與 #2405 取代無法解析的檔用同一個條件）才會在回應中看到項目名稱與 `reason`：單筆的 `error` 文字列出它們，另有 `unreadable: [{path, reason}]`；批次寫在 `message`。其他呼叫者只拿到固定文字與項目數（`unreadable_count`，`message` 裡的 `(unreadable entries: N)`）。給數量與 §3.6「不給數字」不衝突：§3.6 防的是透露看不到的群組成員有幾個，這裡的數量只是壞掉的項目有幾個，不透露任何租戶。非同步批次的結果存在 task 裡、由任何輪詢者讀取，所以一律不含名稱，送出者有全域權限也一樣。不論呼叫者是誰，完整名稱一律寫進 server log。
- **怎麼修**：
  - `TENANT_TREE_UNREADABLE`：在 git 修好或移除那些無法解析的 symlink；若要寫的租戶檔本身就是這種 symlink，直接經 tenant-api 寫入它即可。
  - `TENANT_STORE_FAULT`：從 server log（`conf.d holds unreadable entries`）找出項目，在 tenant-api 的 pod／volume 上移除或修好，或把那份 conf.d 從 git 重設。修好前，經 tenant-api 的寫入都會被拒。
- **監控**：gauge `tenant_api_store_unreadable_entries`、`tenant_api_store_scan_last_success_timestamp_seconds` 與告警 `TenantApiStoreUnreadable`、`TenantApiStoreScanStale`，見 [tenant-api README 的 metrics 表](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/tenant-api/README.md#metricsmetrics)。

### 3.9 每筆寫入先問產生器容器（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）

凡是會改變 conf.d 檔案內容的寫入——`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`，直寫與 PR 模式、同步與非同步——在寫入鎖之下、所有既有檢查之後、commit 之前，tenant-api 把「這次落在哪個 commit 上（直寫是鎖下的 HEAD，PR 模式是分支起點）＋每個要寫的檔的完整新內容」送給同 pod 的產生器容器，由它對寫入後的整棵樹跑 `generate-routes --validate --strict`（[ADR-036 §4](../adr/036-single-parser-effective-config.md)）。批次整批判一次。通過才 commit；其他任何結果都不寫入、不 commit、不留暫存。

- **期限**：給產生器的時間是請求剩下的時間（非同步批次從 task 開始起算 `--write-timeout`）；其中產生器保留的時間（產生器服務的 `--deadline-reserve-seconds`）留給 tenant-api 核對與 commit；剩下的時間不足以判定時直接回 429 `no_time`，不送出。tenant-api 這邊的對應常數與產生器的預設值由 `tests/ops/test_generator_deadline_reserve_parity.py` 對帳。
- **驗的和寫的是同一份**：產生器回報它判的 base commit、每個檔的 sha256 與部署端 policy 檔（`--deploy-policy`，chart 掛同一份）的 sha256，都要和 tenant-api 要寫的一致；不一致就重判一次，仍不一致回 429 `mismatch`。commit 也綁在判定上：直寫的 commit 以 compare-and-swap 推進 HEAD（舊值必須是判定的 base），判定期間 HEAD 被 tenant-api 以外的 commit 移動，回 409（重新整理後重送），不 commit。這條路徑以 `commit-tree` 建 commit（CAS 需要以判定的 base 為 parent），所以不會執行 repo 的 client-side hooks（pre-commit、commit-msg），也不依 `commit.gpgsign` 簽章；批次與 PR 模式仍走 `git commit`。staging 之後 index 裡的內容與判定的位元組逐一比對，`.gitattributes` 改了內容就回 422 `normalized`。直寫單檔只要沒有 commit 成功（CAS 不符、`normalized`、或 git 本身失敗，例如 `index.lock` 被佔用），該檔一律還原成 HEAD 的樣子；index 動不了時仍以 HEAD 的 blob 還原工作目錄的檔案。
- **沒有變更的單檔寫入**：`PUT` 的內容與現有檔案逐位元組相同、且該檔與 HEAD 一致時直接回成功，不問產生器、不 commit。檔案與 HEAD 不一致（外部改了沒 commit 等）時不再判定並 commit，而是回 409 `TENANT_FILE_DIRTY`（見 §3.10）。

| 結果 | HTTP | `code` | 說明 |
|---|---|---|---|
| 有 finding | 422 | `GENERATOR_FINDINGS` | `findings[]` 是產生器的 finding，與這次寫入的檔或租戶相關的排前面並標 `related: true`；`findings_compared_with_base: true` 時每筆另有 `pre_existing`（寫入前就有）。不相關的 finding 只給對所有租戶有寫入權限的呼叫者看，其他人只拿到 `unrelated_findings_withheld`（數量）。批次則是一般的 422 `status: rejected`，每筆結果都是這個 code，訊息列出與該筆租戶相關的 finding |
| 產生器這次沒判出結果，可重試 | 429（`Retry-After`） | `GENERATOR_BUSY` | `reason`：`busy`、`deadline`、`git_read`、`mismatch`、`no_time`（剩下的時間不夠判定） |
| 產生器這次沒判出結果，重送也一樣 | 422 | `GENERATOR_REJECTED` | `reason`：`memory`、`incomplete`、`too_large`（整批寫入後的檔案太大，請拆批）、`invalid_request`、`normalized`（`.gitattributes` 的 eol 或 text 在 staging 時改了內容，commit 的不會是判定過的那份；filter driver 不會走到這裡，見 §3.11）、`unknown`（產生器回了 tenant-api 不認得的 reason） |
| 產生器容器不可用 | 503（`Retry-After`） | `VALIDATOR_UNAVAILABLE` | `reason`：`unreachable`（連不上或期限內沒回應）、`bad_response`、`amtool_missing`、`generator_too_old`、`internal_error` |
| 部署端 policy 檔不可用 | 503（`Retry-After`） | `POLICY_UNAVAILABLE` | `reason`：`policy_unusable`（產生器讀不了）、`policy_unreadable`（tenant-api 讀不了自己掛的那份） |

`GENERATOR_REJECTED`、`GENERATOR_BUSY` 與 503 的回應只帶 code、`reason` 與固定說明，不帶產生器或 tenant-api 的錯誤訊息（可能含路徑、例外內容或其他租戶的檔名）；不認得的 reason 一律顯示 `unknown`。原本的 reason 與訊息只寫進 tenant-api 的 log。`GENERATOR_FINDINGS` 的 `findings[]` 是判定結果本身，依上面的可見性規則揭露。

- **監控**：`tenant_api_policy_available`（產生器容器能否判定，scrape 時讀它的狀態端點）、`tenant_api_generator_rejections_total{reason}`、`tenant_api_generator_scrape_success`，以及附在 tenant-api `/metrics` 之後的產生器 `da_generator_*`；告警 `TenantApiPolicyUnavailable`、`GeneratorJudgementMemoryNearLimit`。
- **寫入扁平鍵 `_routing_receiver_type`** 一律 400，receiver type 寫在 `_routing.receiver.type`：`PUT` 由租戶鍵驗證擋下（`BAD_REQUEST`，訊息 `unknown routing key`），批次 patch 由 patch 鍵驗證擋下（`INVALID_BODY`）。
- **routing 與 domain policy 只由產生器判（breaking）**：tenant-api 不再自己讀 `_domain_policy.yaml`、不再自己判 routing。domain policy 違規（含 `require_critical_escalation`）、receiver 形狀、不是 mapping 的 `_routing`、matcher 與 `group_by` 的值、產生器不讀的鍵、`_domain_policy.yaml` 讀不了，都是上表的 422 `GENERATOR_FINDINGS`（finding 的 `kind` 如 `domain_policy_violation`、`missing_receiver_field`、`routing_not_mapping`、`routing_group_by_invalid`、`routing_value_not_string`、`routing_key_ignored`、`domain_policy_unusable`）。不再有 403 `POLICY_VIOLATION`、tenant-api 自己的 503 `POLICY_UNAVAILABLE`（上表那一列是產生器的部署端 policy 檔，照舊），`PUT` 這幾類也不再回 400；critical 告警可能被提早攔走的警告不再附在 `warnings`。批次 patch 寫 `_routing` 的非停用值不再是 400，合併後同樣交產生器判。
- **部分更新不得改到沒編輯的內容**：批次與 `PUT .../custom-alerts` 是把變更合併進現有租戶檔再整份重新輸出。寫入前把原檔與合併結果都解析成 yaml.v3 節點比對：編輯以外的每個節點寫出來都要相同（種類、style 與引號方式、寫出的 tag、純量原文、anchor、別名所指的內容），文件數與 anchor 也要相同；被編輯的鍵另外解碼確認（設定的是新值、刪掉的不在，`<<` 帶進的也算）。有任何差異（例如 flow 序列裡的 `1:20` 會被重新輸出成 `'1:20'`、block scalar 開頭空行被改、別名共用的租戶 block 連帶被改）就拒收：`TENANT_CONFIG_NOT_LOADABLE`（custom-alerts 回 409；批次是該筆結果的 code），`config_error` 為 `not_round_trippable`，訊息附第一個差異的位置（只在本租戶 block 內才寫出路徑，其他位置只說「另一處」）。合併寫出的字串一律加雙引號（custom-alerts 裡含換行的字串寫成 literal block `|`，讀回來不變時才用）。修法是用整檔 `PUT /tenants/{id}` 或直接在 git 改寫那個檔（[ADR-036 §4](../adr/036-single-parser-effective-config.md)）。
- **解碼失敗的訊息不含檔案內容**：YAML 解碼失敗回給呼叫端的文字只說哪裡失敗與行號，parser 的原文只進 server log（一份檔可能有多個租戶，原文會引用別的租戶的值）。
- **repo 裡不是一般檔的項目**：產生器從 git 物件取檔，symlink（mode 120000）、gitlink（160000）等不是 100644／100755 的項目一律是 `not_regular_file`（路徑中有一段以 `.` 開頭的除外，與所有讀取端略過隱藏項目一致），以 finding 擋下寫入：domain policy 檔名（`_domain_policy.yaml`／`.yml`）報 `domain_policy_unusable`，其他報 `tenant_file_unreadable`，與其他讀不了的檔同一條分類；把它換成一般檔即可。tenant-api 寫該路徑時本來就寫成一般檔（[#2981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2981)）。

### 3.10 租戶檔與 HEAD 不一致時，直寫拒收（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）

**行為變更（breaking）**：直寫模式的寫入——`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`——讀的是 tenant-api 那份工作樹裡的租戶檔（`base_hash` 比對、合併的基底、「沒有變更」的判斷），commit 卻落在 HEAD 上。兩者不同時，這些判斷都是對一份沒有任何 commit 持有的內容下的。所以寫入在拿到寫入鎖、取樣 HEAD 之後、讀檔之前，先請 git 比對這個租戶檔與 HEAD，不一致就回 409 `TENANT_FILE_DIRTY`，什麼都不寫。

- **算不一致的情況**：工作樹有未 commit 的修改、已 `git add` 但未 commit、被刪除、API 要新建的檔名已有未追蹤的檔（含被 `.gitignore` 忽略的）、index 標了 `skip-worktree` 或 `assume-unchanged`（git 不比對這種檔，無從證明一致）；租戶檔經 symlink 抵達時，沿路每個 repo 內的連結與最後抵達的檔都要一致。`.gitattributes` 的正規化由 git 自己處理：`eol=crlf` 下 checkout 出的 CRLF 檔算一致。
- **不判的情況**：symlink 指向這個 repo 以外的檔——repo 外、巢狀 repo 或 submodule 的工作樹內、`.git` 目錄內——git 無從比對，照舊信任工作樹。PR 模式不判：它切的是最新 base 的乾淨 checkout。
- **回應**：`PUT` 類回 409 `TENANT_FILE_DIRTY`，訊息固定、不含路徑；`/diff` 回同一個 409、`/validate` 回 `valid: false` 並帶 `code: "TENANT_FILE_DIRTY"`（git 無法判定時預覽不擋）；批次整批 422、該筆 `code: TENANT_FILE_DIRTY`。重整重試不會成功。
- **以前的回應**：外部的未 commit 修改被當成基底——可能被合併進去並掛在 API 使用者名下 commit，或在內容恰好相同時回成功卻沒有 commit。
- **保證的範圍**：只擋進寫入鎖時已經存在的不一致。檢查到讀檔之間另一個程序再改檔不在保證內；那段時間落地的外部 commit 會讓 HEAD 移動，由 compare-and-swap 擋成 409 `CONFLICT`。
- **怎麼修**：在 tenant-api 掛載的 repo 裡跑 `git status -- <租戶檔>`（skip-worktree／assume-unchanged 用 `git ls-files -v -- <租戶檔>` 看，標記為 `S` 或小寫），把那份修改 commit 或丟棄，再重試寫入。丟棄的方式依狀態而定：已追蹤檔的修改或刪除用 `git restore --staged --worktree -- <租戶檔>`；未追蹤的同名檔（狀態 `??`）、被 `.gitignore` 忽略的同名檔（`git status --ignored -- <租戶檔>` 顯示 `!!`）直接刪除該檔；新 `git add` 但 HEAD 沒有的檔（狀態 `A`）要保留就直接 commit，要捨棄就先 `git rm --cached -- <租戶檔>` 再刪除工作樹的檔（只移出 index 不刪檔，檔會變成未追蹤，重試仍是 409）；標記用 `git update-index --no-skip-worktree`／`--no-assume-unchanged` 解除。

### 3.11 git 設定指名了會執行外部程式的 driver 時，拒絕啟動與寫入（[#3175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3175)）

**行為變更（breaking）**：tenant-api 跑 git 時讀得到的 git 設定裡，只要有下列任何一個鍵，就拒絕啟動；啟動後才出現（例如 init container 或 sidecar 改了 `.git/config`），則拒絕之後的每一筆寫入與預覽。

- **被拒的鍵**：`filter.<名稱>.clean`、`filter.<名稱>.smudge`、`filter.<名稱>.process`、`diff.<名稱>.textconv`、`diff.<名稱>.command`、`merge.<名稱>.driver`。來源不限：conf repo 自己的 `.git/config`、worktree 設定、global（`~/.gitconfig`、`$XDG_CONFIG_HOME/git/config` 或 `GIT_CONFIG_GLOBAL` 指的檔）、system（`/etc/gitconfig`）、經 `include`／`includeIf` 引入的檔、環境變數注入的（`GIT_CONFIG_COUNT`／`GIT_CONFIG_KEY_<n>`、`GIT_CONFIG_PARAMETERS`，scope 顯示為 `command`）。同一 section 下不指名程式的鍵（`filter.<名稱>.required`、`diff.<名稱>.binary`、`merge.<名稱>.name`）不擋。**沒有允許清單**。
- **為什麼**：這些鍵指名的程式，git 會在處理 `.gitattributes` 選中的檔案時執行——`git add`、`git status` 跑 clean filter，`git checkout` 跑 smudge，`git diff`／`git log -p` 跑 textconv 或外部 diff。程式本身由 git 設定決定（operator 控制），但**對哪些檔、何時執行由 `.gitattributes` 決定，能 commit 到 conf repo 的人就能改**。而 `POST /tenants/{id}/diff`、`/validate` 的預覽會跑 `git status`（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535) 的租戶檔一致性檢查），只需要讀權限；不擋的話，只有讀權限的人也能讓 tenant-api 反覆執行這些程式。
- **用 git-lfs 或 git-crypt 的 config repo 不能接 tenant-api**：兩者都靠 filter driver（`git lfs install` 會把 `filter.lfs.*` 寫進 global 設定）。tenant-api 不以 `-c filter.<名稱>.clean=` 之類的設定把 driver 關掉再繼續：那樣 git 會把工作樹的位元組原樣入庫（lfs 指標、git-crypt 的明文），且被 filter 的檔全部判成有未 commit 的修改。
- **core.fsmonitor**：tenant-api 的每一個 git 指令都帶 `-c core.fsmonitor=false`，設了也不會執行；這個設定經 `GIT_CONFIG_PARAMETERS` 也傳到 hook 裡跑的 git 指令。關掉它不改變任何結果，只是 git 自己掃工作樹。
- **diff.external／`GIT_EXTERNAL_DIFF`**：這兩者只靠設定或環境變數就指名外部 diff 程式，不需要 `.gitattributes`，也不在上面被拒的鍵裡；tenant-api 的 `git diff`（`/diff` 預覽的 `--no-index`、寫入時的 `--cached --quiet`）與 `git log -p` 一律帶 `--no-ext-diff --no-textconv`，設了也不會執行（`--no-textconv` 是 textconv 鍵被拒之外的第二層）。
- **hooks 照常支援**：conf repo 的 client-side hooks（`pre-commit`、`commit-msg`、`post-commit`、`pre-push`、`reference-transaction`、`post-checkout`、`post-index-change`）照舊在 tenant-api 自己的 git 指令中執行。信任模型不同：hook 放在 `.git/hooks`（或 `core.hooksPath` 指的目錄），只有能改 tenant-api 那份 repo 的檔案系統或 git 設定的人才放得進去，commit 內容選不了要跑哪個 hook，也不會因預覽而執行。代價也一樣要知道：hook 以 tenant-api 的身分、在寫入鎖之下執行，受 `TENANT_API_GIT_TIMEOUT` 約束；直寫單檔以 `commit-tree` 建 commit，不跑 `pre-commit`／`commit-msg`（§3.9）。
- **條件式 include**：`includeIf "onbranch:…"` 依 HEAD 指的分支、`includeIf "gitdir:…"` 依 repo 目錄決定是否生效，所以結果取決於檢查時的情境。寫入鎖下的那次檢查，查的是拿鎖當下 HEAD 所在的分支——不一定是 base（上一筆 PR 寫入回不了 base 而留下的分支、operator 自己 checkout 的分支）。因此每次切換分支之後、git 在新情境下碰檔之前，都在新情境再查一次：切回 base 之後（PR 寫入與批次錨定 base、PR 寫入結束或中止時回 base、直寫從殘留的 PR 分支回 base）；PR 寫入切到 feature 分支後、寫檔與 `git add` 之前；批次在 staging worktree 以 `worktree add --no-checkout` 建好之後、填 index 與 checkout 之前（linked worktree 有自己的 git 目錄，PR 批次還有自己的分支）。在 feature 分支被拒時，HEAD 先指回 base 再還原工作樹，不在 feature 分支的設定下碰檔，分支與 worktree 都清掉；在 base 被拒時，工作樹留在 base（切過去的那次 checkout 跑在原分支、已查過的設定下），不再跑任何碰檔的 git，下一筆寫入在寫入鎖下就會被拒（PR 寫入在 commit 之後回 base 才查到的，該筆照常完成：分支已 commit、可能已 push，剩下的步驟不碰檔）。
- **偵測本身不執行任何程式**：以 `git config --show-scope --get-regexp` 讀設定（與 tenant-api 平常跑 git 同一個環境、同一個 repo），這個指令只讀設定檔，不跑 driver 也不跑 fsmonitor。
- **回應**：啟動時直接結束，`FATAL:` 訊息列出每個鍵的 scope 與鍵名（例如 `local filter.lfs.clean, command diff.x.textconv`），**不印值**——值是一段指令，可能含路徑或憑證。執行中被拒的寫入（`PUT`、custom-alerts、批次、PR 模式）回 500 `GIT_DRIVER_CONFIGURED`（[#3226](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3226) 起；原本是 `INTERNAL_ERROR`），訊息固定、帶 `request_id`、不帶 `Retry-After`，scope 與鍵名只進 server log；async 批次的 task 每一筆都是 `GIT_DRIVER_CONFIGURED`；`/diff` 同樣回 500；`/validate` 回 `valid: false` 與固定訊息（PR 模式的 `/validate` 只做不讀樹的 body 檢查、不跑 git，不會因此被拒）；PR 模式的租戶列表不推算依設定的狀態（`config_derived` 帶原因，只有不受範圍限制的讀者看得到 scope 與鍵名，與其他載入錯誤相同）。設定有 driver 時重試不會成功。設定讀不出來（git 失敗或逾時、設定檔或其 include 格式錯、repo 目錄不存在）也一樣拒絕——讀不到就無從證明沒有——但那不代表有 driver，答案分開（#3226）：啟動時 `FATAL:` 訊息說的是「讀不到設定」；寫入回 503 `UPSTREAM_ERROR`、`Retry-After: 5`，訊息固定，下一次寫入會在寫入鎖下重讀（問題持續就持續 503）；PR 寫入 commit 之後回到 base 才讀不到的，該筆照常完成（tree 已在 base，之後每個會碰檔的 git 前都會再查）。預覽遇到這種情況則與 git 失敗時相同，略過與 HEAD 的一致性檢查、不跑 `git status`。
- **怎麼修**：在 tenant-api 的容器裡（同一個使用者與環境）跑 `git -C <conf repo> config --show-scope --show-origin --get-regexp '^(filter\..+\.(clean|smudge|process)|diff\..+\.(textconv|command)|merge\..+\.driver)$'`，依 scope 與來源檔把那些鍵刪掉（`git config --unset`，或改 global／system 設定檔、移除注入的 `GIT_CONFIG_*` 環境變數），再重啟 tenant-api。

---

## 4. 升級指引

### 4.1 Production rollout

| 階段 | 動作 | 風險 |
|---|---|---|
| 1. Deploy v2.8.0 | 預設 `TA_RATE_LIMIT_PER_MIN=100`；Groups/Views/Task/PR 開始強制 tenant-scoped authz | 同一呼叫者（`X-Forwarded-Email`，沒有則來源 IP）任一 60 秒內第 101 個請求回 `429`；只有 `/health`、`/ready`、`/metrics` 不計，SSE 每開一條連線也算一次。自動化巡檢腳本可能踩到限流 |
| 2. 監測 24h | grep `429` 比例；確認沒有合法用戶被擋 | — |
| 3. 調參 | 若特定批次工具需要更高 budget，調 `TA_RATE_LIMIT_PER_MIN`（建議 100 → 250 → 500 step-up）| — |
| 4. 客戶 RBAC 補完 | 若有 group/view 跨團隊共用 → 補完成員租戶的 RBAC 授權 | 不補的話 PUT/DELETE 會 403 |

### 4.2 客戶端應對

舊版 client 會看到的新行為：

- **新標頭**：`X-Request-ID` 出現在所有回應 → 可選擇 log / 不 log，沒有 breaking
- **新狀態碼**：`429`（rate limited）→ client 應 honor `Retry-After` 標頭，指數退避重試
- **新 403**：對 cross-tenant group 操作 → client 應 surface 訊息給 user（已含完整禁用 tenant 列表）

無 breaking change 對 v2.7.0 既有 happy-path API 客戶端 — 只多兩種 error case。

### 4.3 Pre-prod / open-mode 環境

無 `_rbac.yaml` 的開發環境：
- Reads 仍然全通（v2.7.0 行為不變）
- **Writes 開始要求 `_rbac.yaml`**：必須補 `groups: [{name: dev, tenants: ["*"], permissions: [admin]}]` 之類的最小配置

未補 → PUT/DELETE Groups 在新 tenant-scoped check 處 403。修補本身是 ~5 行 YAML，不擋 v2.7.0 → v2.8.0 升級。

---

## 5. 已知 gap（不在本次硬化範圍）

### 5.1 ~~Body 內容範圍校驗~~（C4 ✅ landed v2.8.x via [issue #134](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/134)）

**Status**：v2.8.x hardening PR 落地。`POST /api/v1/tenants/batch` / `PUT /api/v1/groups/{id}` / `PUT /api/v1/views/{id}` body 已加入 `go-playground/validator` + struct tag；Patch 的 key 由 threshold-exporter 的保留鍵登錄表（`reserved_keys.json`，[ADR-038](../adr/038-unread-keys-judged-by-readers.md)）判定，保留鍵的值由寫入關卡以 exporter 自己的讀取函式判定（[#3227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3227)）。

**驗證範圍**：

| 欄位 | 規則 |
|---|---|
| `BatchRequest.operations` | 1-1000 entries |
| batch request body 總量(`POST /tenants/batch` **與** `POST /groups/{id}/batch`) | ≤ 256 KiB(`TA_MAX_BATCH_BODY_BYTES`,超量 413 `PAYLOAD_TOO_LARGE`) |
| 單一租戶文件 | ≤ 64 KiB(`TA_MAX_TENANT_DOC_BYTES`,超量 400)。量的是寫入路徑實際會解析的那一份 —— 合併路徑(batch patch)上是合併後的整份檔案,`PUT /tenants/{id}` 與 dry-run 上是請求自己的 bytes |
| `BatchOperation.tenant_id` | required, 1-256 chars |
| `BatchOperation.patch` / `GroupBatchRequest.patch` key 數 | ≤ 1000 entries(bytes 不是這裡的正確單位:合併對 key 數是二次的,且跑在 single-writer token 內) |
| `BatchOperation.patch` 一般 key/value | key ≤ 256 chars, value ≤ 1024 chars |
| `_silent_mode`（`PUT /tenants/{id}`、batch、dry-run 皆同） | 由寫入關卡以 exporter 讀取 `_silent_mode` 的同一個函式判定，不在 handler 另抄一份詞表（[#3227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3227)）。接受 `warning`／`critical`／`all`，停用字 `disable`／`disabled`／`off`／`false` 與空值；大小寫不敏感、前後空白會先去掉；結構化形式（`target:`＋選填 RFC 3339 `expires:`、`reason:`）同樣由讀取端解析。exporter 會忽略的值（`purple`、不認得的 `target`、不是 RFC 3339 的 `expires`）：`PUT` 回 400 `BAD_REQUEST`；batch 是該筆 op 失敗（422，op `code: BAD_REQUEST`，整批不寫；`?async=true` 為任務失敗）。⚠️ 租戶檔裡**既有**的錯值，只要這次寫入沒有改變它（以讀取端解析結果比較：加引號、改大小寫、改鍵序，以及 `"target: purple"` 字串與同內容的 mapping 互換，都算沒變；純量 `purple` 改成 `{target: purple}` 則讀取端的判定不同，算改了），就不擋寫入，改在 notices 回一則 `NOTICE:`；`GET /tenants/{id}` 也把它列在 `validation_notices`，不在 `validation_warnings`。PR 模式的 dry-run 只跑 body-only 預檢，不判這個值（[#1718](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1718)） |
| 登錄表沒有的 `_` 開頭 key | 一律 400 `INVALID_BODY`，reason `unknown reserved key (typo?); nothing reads it`（例如已不存在的 `_timeout_ms`、拼錯的 `_silent_mod`）。沒有任何一層讀它（[#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)） |
| 值是 mapping 或 list 的保留鍵（`_metadata`、`_custom_alerts`、`_namespaces`） | batch patch 的值是字串，設不了它們：400 `INVALID_BODY`，reason 指出形狀並指向 `PUT /tenants/{id}`（`_metadata` 沿用 [#2859](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2859) 的原文） |
| `BatchOperation.patch._routing_profile` / `_profile` | 一般 value 上限 1024 chars（原本另有 256 上限，已移除）。`_profile` 指向的 profile 是否存在，由寫入關卡的租戶鍵驗證判（`_profile references unknown profile`，400／該筆 op 422）；`_routing_profile` 由 route generator 對寫入後的整棵樹判 |
| `BatchOperation.patch._routing` | tenant-api 不判它的值（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）：停用字串 `disable`／`disabled`／`off`／`false` 以外的值由 route generator 對寫入後的整棵樹判，回 422 `GENERATOR_FINDINGS`（見 §3.9）。要重新啟用請用 PUT 寫入 `_routing` mapping，或在 batch 用 `unset: ["_routing"]` 移除（#2341）|
| `BatchOperation.patch` 其他 `_routing*` key | `_routing`、`_routing_profile` 以外以 `_routing` 開頭的 key（拼錯的 `_routing_profle`、扁平鍵 `_routing_receiver_type`、寫在租戶裡的 `_routing_defaults`）一律回 400 `INVALID_BODY`，在 domain policy 判定之前；`field` 形如 `operations[0].patch["_routing_profle"]`。產生器不讀這些鍵（[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)） |
| `BatchOperation.unset` / `GroupBatchRequest.unset` | 要從租戶 block 刪除的 key，≤ 1000 entries；目前只接受 `_routing` 與 `_routing_receiver_type`（其他 key 回 400；清單由登錄表推導：`batch.unset` 的鍵加上所有已退役的鍵）。後者是 tenant-api 以前會寫入的扁平鍵，產生器不讀；它留在租戶檔裡時，這個租戶的其他寫入都會被 key 驗證擋下，所以要先刪掉它（[#2983](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2983)），刪除不受 domain policy 判定。刪掉 `_routing` 等於回到 `_routing_defaults` + routing profile，也就是重新啟用被停用字串關掉的路由，因此和 `_routing` patch 一樣受 domain policy 判定（違反的那筆被拒；批次全有全無，整批不寫入，[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)）；mapping 型的 `_routing` 也可以刪（patch 不能覆寫 structured key，unset 不受此限）。租戶沒有這個 key、或租戶根本不存在時是 no-op（比照 RFC 7396 刪除不存在的成員）：沒有 `patch` 的話不寫入、不 commit、不開 PR，也不會因此建出租戶，回報與其他沒有變更的 op 相同（直寫模式 `ok`，PR 模式整批皆無變更時回 `completed` 與 `No changes to apply; no PR/MR created.`），也不會因磁碟上既有的 routing 被 domain policy 擋下。`patch` 與 `unset` 皆空時，`/tenants/batch` 同樣是 no-op、什麼都不寫（B2 之前會替沒有檔案或沒有該租戶段落的租戶補上 `<id>: {}`，並讓縮排不是 2 格的檔案產生只重排格式的 commit），`/groups/{id}/batch` 維持原本的 400 `BAD_REQUEST`（`patch must not be empty`）。domain policy 判的是產生器會算繪的路由：租戶 block 疊在 root 平台檔的 `tenants.<id>` overlay 之上（租戶自己的 `_routing`／`_routing_profile` 整個勝出），policy 讀 `_domain_policy.yaml` 與 `.yml`（同名 domain 以 `.yml` 為準）。PR 模式（batch 與 `PUT /tenants/{id}`）在切出分支的最新 base 上再判一次，以上輸入都讀 base 上的版本：pod 本機落後 base、本機放行的寫入在 base 上違規，或 base 上的 policy 檔無法載入時，PUT 回 403 `POLICY_VIOLATION`（帶 `tenant_id`），batch 回 422 該筆結果 `code: POLICY_VIOLATION`（整批不寫，[#2900](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2900)），不寫入、不開 PR、不留分支。同一 key 不可重複、不可同時出現在 `patch`——違反皆回 400 `INVALID_BODY`，`field` 形如 `operations[0].unset[1]`（group batch 為 `unset[1]`）。`unset` 不是陣列時是 JSON 解碼錯誤（400 `BAD_REQUEST`）。不採 RFC 7396 的 `null` 刪除語意：spec 是 Swagger 2.0，表達不了 nullable，生成的 client 送不出 `null` |
| `PutGroupRequest.label` / `PutViewRequest.label` | required, 1-256 chars |
| `PutGroupRequest.description` / `PutViewRequest.description` | ≤ 4096 chars |
| `PutGroupRequest.members` | 0-1000 entries, each 1-256 chars |
| `PutGroupRequest.filters` / `PutViewRequest.filters` 項目數 | ≤ 20 entries（view 另外 required、至少 1 個）。transform 跑在 state store 的讀改寫內（輸掉並行寫入會重跑），且結果寫進 state store 的 `_groups.yaml` / `_views.yaml` 紀錄 ⇒ 劣化是永久的 |
| `Filters` map values | ≤ 1024 chars per value |
| `PUT /tenants/{id}` body 自己寫的 receiver（`_routing.receiver`、`overrides[].receiver`、`routes[].receiver`） | tenant-api 不再自己判（[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）：receiver 契約、產生器不讀的鍵、未知的 `_routing*` 鍵都由產生器對寫入後的整棵樹判，回 422 `GENERATOR_FINDINGS`（見 §3.9）。Go 端只剩扁平鍵 `_routing_receiver_type`（租戶鍵驗證，`BAD_REQUEST`）與遮罩佔位字串（`INVALID_BODY`）兩項 400 |

**Failure response shape**：

```json
{
  "error": "validation failed",
  "code": "INVALID_BODY",
  "violations": [
    {"field": "operations[0].patch[\"_timeout_ms\"]", "reason": "unknown reserved key (typo?); nothing reads it"},
    {"field": "operations[1].patch[\"_namespaces\"]", "reason": "_namespaces is a list and a batch patch value is a string, so a batch cannot set it; write it as a list with PUT /api/v1/tenants/{id}"}
  ]
}
```

ALL violations 全列出（不是 first-only），跟新 tenant-scoped check 的 forbidden-tenant 列表 UX 一致 — 客戶一個 round-trip 就能 fix 所有問題。

### 5.2 Server-level timeout / body-size config — moved to Helm (v2.9.0, #144)

`http.Server{ReadTimeout, WriteTimeout, IdleTimeout}` 與 per-handler body cap 已從 hardcoded 改為 `TA_READ_TIMEOUT` / `TA_WRITE_TIMEOUT` / `TA_IDLE_TIMEOUT` / `TA_MAX_BODY_BYTES` env-driven，並透過 `helm/tenant-api` `tenantApi.server.{timeouts.{read,write,idle},maxBodyBytes}` values 暴露。預設值對齊 v2.8.0 原 hardcoded 值（15s / 30s / 60s / 1 MiB），default upgrade 為 no-op；env malformed → `slog.Warn` + fallback。

### 5.3 SSE client liveness — heartbeat + per-write deadline（#143）

**已解決（#143）。** `/api/v1/events` SSE hub 過去沒有 per-client liveness 機制：卡住 / 半開的 client 會無限期佔住 serving goroutine。原本提議的「idle timeout 到時關線」對單向 SSE 是錯的設計（server→client 沒有 client read activity 可量、且會打健康的閒置連線），且會與 §5.1 的全域 `WriteTimeout` 互打。改採標準 SSE liveness 模式：

- **豁免全域 `WriteTimeout`**：handler 以 `http.NewResponseController(w).SetWriteDeadline(time.Time{})` 清掉 server 的全域寫入 deadline。否則長連 SSE 會在連線後 ~`TA_WRITE_TIMEOUT`（預設 30s）的第一次寫入時被砍斷。
- **Heartbeat**（`TA_SSE_HEARTBEAT`，預設 25s）：週期性寫 `: keepalive` SSE comment。兼具兩個作用 —— (1) 防中介 proxy/LB 收掉閒置連線；(2) **load-bearing**：保證週期性的寫入嘗試，讓 per-write deadline 有機會對「閒置零流量」的卡死 client 觸發（goroutine 卡在 `<-ch`、兩次 heartbeat 之間沒有 in-flight 寫入時，deadline 是 dormant 的）。**`0s` = 停用，會重新打開 idle-stuck-client leak**；且必須 < 下游 proxy 的最小 idle timeout。
- **Per-write deadline**（`TA_SSE_WRITE_TIMEOUT`，預設 10s）：每次寫入前設 `SetWriteDeadline`。卡住 client 的寫入最多 block 這麼久就 error → serving goroutine return → 資源回收。worst-case 卡死 client 清除 ≈ `heartbeat + write-timeout`（~35s）。**維運注記（反壓緩衝）**：這 ~35s 是**下限**而非上限 —— 前面若擋著 Nginx / HAProxy / Ingress（各有數十~數百 KB response buffer），client TCP 半開後 exporter 的寫入會先灌進 OS + proxy buffer、不會立刻 block，要等那些 buffer 也滿、TCP backpressure 才傳回來。goroutine 最終仍會回收，只是比 ~35s 晚。若 `tenant_api_sse_clients` 在斷線後下降得比預期慢，是這個 buffering（非 leak）。
- **可選硬上限**（`TA_SSE_MAX_LIFETIME`，預設 `0s`=停用）：單一連線的最長存活時間（defense-in-depth），到時送 `{"type":"close"}` 後關線、由 well-behaved client 重連。
- **可觀測性**：`/metrics` 新增 `tenant_api_sse_clients` gauge（目前連線數 == serving goroutine 數）；穩定 client 數下持續攀升即 leak 訊號。

三個 env 在 `helm/tenant-api` 以 `tenantApi.sse.{heartbeat,writeTimeout,maxLifetime}` 暴露（預設對齊 binary built-in，default upgrade 為 no-op）。malformed env → `slog.Warn` + fallback。

### 5.4 Git CLI per-command timeout（#630）

GitOps 寫入（`Write` / `WritePR` / `WritePRBatch`）全程持一把 process 級寫入鎖 `sync.Mutex`，期間呼叫的 git CLI 子程序原本無逾時 —— 卡住的 `git push`（degraded on-prem forge / 網路瞬斷）會無限期持鎖、凍結**所有**租戶寫入直到 pod 重啟。現每個 git 呼叫都有 per-command deadline（`exec.CommandContext` + `WaitDelay`，後者確保即使 `git-remote-https`/`ssh` helper grandchild 仍持 stdout pipe 也能釋鎖），逾時即 SIGKILL、回 loud `timed out — write lock released` 並釋鎖。預設 60s，由 `TENANT_API_GIT_TIMEOUT`（Go duration，如 `90s`）覆寫、`helm/tenant-api` `tenantApi.gitTimeout` value 暴露；非法 / 0 / 負值 fallback 回預設。

**逾時之後寫進去了沒有（[#3173](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3173)）**：git 指令自己的逾時是獨立錯誤，不再與請求取消混在一起。請求取消或撞到請求期限時，writer 只在檢查點停下、從不殺 git，所以一定沒寫入。git 逾時則由 writer 判斷：`add`、`write-tree`、`commit-tree`、租戶檔與 HEAD 的比對（§3.10）都不動分支，逾時就是沒寫；只有推進分支的 `update-ref` 可能在被殺之前已經寫下，writer 會重讀分支——HEAD 讀不到時直接讀 `refs/heads/<分支>`——再與寫入前記下的 commit 比對。直寫（單租戶與 batch）因此有三種結果：

| 結果 | 何時 | 回應 |
|---|---|---|
| 確定沒寫 | 請求取消或期限到；git 逾時但分支沒動 | 503，`the write was cancelled or timed out before it was committed; nothing was written, so it is safe to retry` |
| 確定已寫 | 分支已在這次的 commit 上 | 照成功回應；batch 的工作樹沒跟上時仍是 409 `committed: true` |
| 未知 | `update-ref` 回錯，且 HEAD 與分支 ref 都讀不到 | 503，`… it may have been committed — refresh to see whether it was before retrying`；非同步 batch 的 task `error` 同句，不寫 `nothing was written` |

分支被別人移到第三個 commit 時仍是 409 衝突。單租戶直寫在寫服務中的租戶檔**之前**就寫下 publish marker（`<git common dir>/tenant-api-batch-publish`），直到這個檔回到已知狀態（已發布、與 HEAD 相同、或已照 HEAD 放回）才刪掉，所以結果未知、或任何一步失敗而檔沒能放回時（含程序被殺）它都還在；batch 則在結果未知時保留它。下一次寫入（或重啟）讀得到 HEAD 時由它收尾（結果判定可退回讀分支 ref，收尾則一定要讀得到 HEAD）：commit 已在分支上，就把工作樹的這些檔與 index 帶到分支上；不在（或單租戶寫入還沒走到 commit），batch 的工作樹本來就沒動，單租戶寫入已寫出的服務中檔案則照 HEAD 放回（index 仍記著這次 commit 的內容時一併重設），該租戶不會一直卡在 `TENANT_FILE_DIRTY`。HEAD 一直讀不到（或收尾本身一直失敗）的期間，每一次寫入都失敗，錯誤訊息指出這個 marker。收尾會覆寫這段期間對該檔的手動修改；PR 模式的寫入不在這個 marker 的範圍內。PR 模式在 push 之前不動 base 分支，push 本身的失敗另有 `GIT_PUSH_FAILED`（§5.5）。

### 5.5 PR-mode checkout 紀律 + SIGKILL 殘鎖自癒（#638）

§5.4 的逾時 SIGKILL 衍生的兩處寫入路徑硬化：

- **De-relativize checkout（防跨租戶分支污染）**：`WritePR`/`WritePRBatch` 原本從「當前 HEAD」`checkout -b` 並靠相對 `checkout -` 切回。若工作區被前次寫入留在某 feature branch，下一個租戶會**從別人的 feature branch 分叉**、PR 靜默夾帶他人未推送設定。現在每次 PR 寫入**開頭以 ironclad `reset --hard HEAD` + `checkout -f <base>` 洗白**再 `-b`、所有切回改用同一 clean checkout，污染**不可能發生**（任何 stuck 狀態下次自我矯正）。**為何是 ironclad 而非 plain `checkout`**：寫檔成功但 commit 未完成即被 SIGKILL 會留 dirty tree，plain `checkout <base>` 會被擋（"local changes would be overwritten"）→ wedge 住後續每個 PR 寫入，PVC-backed conf.d 連 pod 重啟都解不開（death-loop）。base 由 `TA_GIT_BASE_BRANCH`（預設 `main`、**forge-neutral**、`--git-base-branch` flag）決定；base 不可達即 abort（不從未知 ref 分叉）。
- **分支名唯一＋push 失敗不開 PR（[#1724](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1724)）**：PR 分支名是 `tenant-api/<租戶>/<UTC 秒>-<12 位十六進位亂數>`（批次為 `tenant-api/batch/<UTC 秒>-<亂數>`），同一秒的兩次寫入（例如兩個不同租戶組合的批次、一次被拒後立刻重試）不再撞名。push 失敗——被拒、連不上、逾時都算——寫入回 503 `GIT_PUSH_FAILED`＋`Retry-After: 5`，訊息固定（`pushing the change to the config repository failed; no PR/MR was opened — please retry`），不開 PR/MR，批次整批同一個錯誤信封、不回 `pending_review`；遠端位址與 git 輸出只進 server log 的 `PR-mode write: push of the feature branch failed` 一行，附 `request_id`。本機分支刪掉（刪除失敗只記 log，不影響回應）、worktree 回到 base，重試會切新的分支；例外是切回 base 失敗（重試一次仍失敗）：worktree 還停在 feature 分支上，分支無法刪除，回 500 `BASE_RESTORE_FAILED`，下一次寫入會把工作樹拉回 base（若那是直寫模式的寫入，它本身會被拒）（以前 push 失敗保留本機分支當作「唯一一份 commit」，但 commit 本來就能由重試重建，留下的同名分支反而讓同一秒的下一次寫入 500）。PR 路徑上 push 之前的 git 指令逾時（`TENANT_API_GIT_TIMEOUT`）或請求被取消，回 503「沒有寫入，可以重試」，不再是 500：push 之前什麼都還沒到 origin（§5.4 的「確定沒寫」）；例外是 base 的 fetch 逾時回 503 `FORGE_UNAVAILABLE`，切回 base 的逾時則屬上述的 `BASE_RESTORE_FAILED`。**已知限制**：push 逾時但遠端其實已收下時，origin 會留下一條沒有 PR/MR 指向的 `tenant-api/` 分支；tenant-api 不會自己清，請定期以 `git ls-remote origin 'refs/heads/tenant-api/*'` 對照開著的 PR/MR 後刪除。
- **SIGKILL 殘鎖自癒**：被逾時 SIGKILL 的本地 `git add`/`commit` 會留 `.git/index.lock`（及 `HEAD.lock`、`refs/**/*.lock`、`packed-refs.lock`、`config.lock`）；因所有寫入共用 `sync.Mutex`，一個殘鎖會讓後續每個租戶寫入都 `index.lock: File exists` 直到人工介入。`gitErr` 的 deadline 分支現在 best-effort 清掉這些鎖 —— 安全性僅來自 mutex 序列化 + conf.d 由單一 replica 獨占（當下無並發 git 持鎖）。

### 5.6 部署策略 `Recreate` 與 SSE 重連（read-HA trade-off，#677 / #675 / #740）

寫平面是**單寫者**（ADR-023）。為消除滾動更新交疊期的「幽靈副本」多寫者 correctness bug（#677），tenant-api Deployment 採 **`strategy: Recreate`**（殺舊 pod 再起新 pod，無交疊）。**對價**：每次部署都會結束所有開啟中的 `GET /api/v1/events` SSE 連線。

- **優雅關機（#675）**：SIGTERM 時 `Hub.Shutdown` 先廣播一則 `server_shutdown` 控制事件、再關閉所有 SSE 連線，**然後**才 `http.Server.Shutdown`。兩個效果：(1) SSE 連線永不 idle，若不先關，`srv.Shutdown` 會卡滿 15s grace period 才硬切——先關讓 Shutdown 毫秒級完成；(2) client 收到的是**可行動的訊號 + 重連提示**而非生硬的 connection reset。事件契約：`{"type":"server_shutdown","reconnect_delay_ms":2000}`——well-behaved client 應等 `reconnect_delay_ms` **加自身隨機 jitter** 再重連，把重連流量打散離開尚未 ready 的新 pod。Shutdown 一旦開始，在 `srv.Shutdown` 之前抵達的 late request 會被拒（`/api/v1/events` 回 **503**），避免它開一條錯過 hint、又拖住關機的新串流。〔前端 jitter 重連屬 Portal 對接 future work——目前無 SSE consumer，server 先發出契約事件作為基礎。〕
- **預期行為（自癒）**：SSE client 標準會自動重連；hub 已用 heartbeat + per-write deadline 硬化（#143）。**單次部署的重連是預期且自癒的**，讀取中斷僅數秒。`tenant_api_sse_clients` gauge 在重連完成後回升。
- **觀測護欄（#740）**：alert `TenantApiSSEReconnectFailure` 偵測「重連**失敗**」—— 三條件 `for: 10m`（皆對 `tenant_api_sse_clients` / `tenant_api_uptime_seconds` 做 `sum/min without (pod, instance, endpoint)` 聚合，見下）：聚合 client 數 `== 0`（現無連線）且 **`min(uptime_seconds) < 1800`**（pod 近 30m 內**重啟過** —— load-bearing：把告警錨在部署窗，否則無法區分「重連失敗」與「使用者正常關掉 Tenant-Manager 分頁」，低頻 admin UI 會狂誤報）且 `max_over_time(sum(...)[30m:1m]) > 0`（近 30m 有過 client、確有東西該重連）。**為何聚合**：Recreate 換 pod 後若 scrape 帶 per-pod label（如 ServiceMonitor / endpoints role），新 pod 是不同 series、其 30m 歷史全 0，PromQL `and` 需精確 label match → alert 會**靜默永不觸發**。把易變 label 聚合掉（單寫者＝單 pod，安全）讓新舊 pod 合成單一邏輯 series、歷史橫跨重啟，對**任何 scrape role 都正確**。正常單次重連（秒級回升）不誤報；無人連線不誤報；非部署期關分頁（uptime 大）不誤報；client 回連 / uptime 過 30m 即自動 resolve。行為契約由 `tests/rulepacks/platform-sse-reconnect_test.yaml` 的 promtool 測試（4 場景，**含雙-pod 跨重啟**）鎖定。
- **read-HA 是 deferred（#678 / #788，已關閉並 codify re-trigger）**：給讀取 zero-downtime 的正解是讀寫拆分部署（read deployment 多副本 RollingUpdate + binary read-only enforcement、write deployment 維持單副本 Recreate）。**刻意 defer**——成本中等，且「讀取 HA 是需求」這個前提目前**無 field data 驗證**（讀路徑為低頻 admin UI、發版數秒 blip 未經量測證實有害）；在需求成形前先蓋＝服務一個幻影需求。為免 defer 流為「關掉的 issue 裡一句沒人看的 TODO」，trigger 已**codify 成會自己叫的 alert** `TenantApiReadHANeeded`（severity `info`、不 page）：對同一個 `tenant_api_sse_clients` gauge 取 **7d 平均並發 > 2**（`avg_over_time(...[7d:1h])`，取平均非峰值＝只認「持續多人」、不被「單日尖峰」誤觸；另加 `count_over_time(...) >= 150` 守 cold-start，因 `avg_over_time` 只平均「已存在」的樣本，fresh deploy 的 partial <7d 視窗否則會在沒有真正 7d 基線前就誤觸）即「read-HA 成為真實需求」、屆時 reopen ADR-023 Deferred A4 實作讀寫拆分。**⛔ 觸發時注意**：A4 單做只買到**讀** zero-downtime，**寫**路徑（Save）發版仍中斷，需與 A3（K8s Lease，#787）**同排**、勿單獨出 A4。互補可一併看 `rate(tenant_api_requests_total)`（讀為主；寫入是極小的 `sum(rate(tenant_api_writes_total))`，三個 `result` 合計，#3054）。行為契約：`tests/rulepacks/platform-read-ha-trigger_test.yaml`。在那之前 Recreate 的數秒讀取 blip 是接受的對價。

### 5.7 執行期單寫者破口偵測（runtime multi-writer，ADR-023 L3 / A3 / #787）

§5.6 與 L1（Helm `fail` guard + `check_single_writer_invariant.py`）守的都是**部署期**：replicaCount==1、`strategy: Recreate` 為字面硬編碼、無 autoscaler manifest。但這些都是**靜態檢查**，只綁「設定撰寫」向量。三條**執行期**路徑會把 live Deployment 的副本數拉 >1 並**完全繞過** L1/L2：`kubectl scale --replicas=2`、controller（KEDA）在執行期生成的 HPA、或 GitOps controller reconcile 一個被手改的 `replicas`（尤其當 `replicas` 被放進 `ignoreDifferences` —— 一個會悄悄廢掉 L1/L2 的常見 footgun）。兩個 pod 同時對同一個 git working tree push → tree 損毀 + **無聲資料遺失**。執行期的唯一正解是 L3（K8s Lease / leader-election，A3 / #787），已 defer。

- **偵測護欄（本節）**：alert `TenantApiSingleWriterBreach`（severity `critical`、會 page）把 trigger 從 **lagging**（等資料毀損事故才發現）改成 **leading**。expr 是**兩個 KSM gauge 取 `or`**、`for: 2m`：`max by (namespace, deployment) (kube_deployment_spec_replicas{...tenant-api}) > 1 or max by (namespace, deployment) (kube_deployment_status_replicas{...tenant-api}) > 1`。**兩條腿**：`spec_replicas` = **意圖**（宣告值，L1 `replicaCount==1` guard 的執行期鏡像，`kubectl scale` / HPA / GitOps-patch 一改 intent 就抓到，最早觸發）；`status_replicas` = **現實**（實際非終止 pod 數，抓「spec 仍是 1 但實際跑了 2」的背離——如 node-eviction 替補 pod 撞上一個卡在 Terminating 的舊 pod；`strategy: Recreate` 健康部署永不 surge 到 2，故此腿無正常誤觸）。**誠實的殘留（不誇大為密不透風）**：兩腿都**擋不住網路分割的 ghost pod**——被分割節點上的舊 pod 仍連得到 git/使用者卻對 control plane 隱形，而 KSM/Prometheus 讀的正是 control-plane 狀態，in-cluster 觀測者看不到被完全分割的節點。這個不可化約的殘留**正是** L3（K8s Lease + git push 的 fencing token，A3 / #787）才是唯一真解的理由——這張雙 gauge 網**收窄**破口、並未封死。**為何聚合**：`max by (namespace, deployment)` 把 scrape 帶的 `job`/`instance` label 收斂成單一邏輯 series（也撐過 KSM rescrape 換 instance），免得雜散 label 讓 `> 1` 比對**靜默永不命中**（同 §5.6 SSE 告警的 anti-silent-fire 紀律）。**為何 `for: 2m`**：debounce GitOps self-heal 在 sync loop 內把手動 scale revert 回 1 的瞬態，但**刻意不**拉長到「蓋過自癒週期」——對「靜默損毀」不變式，持續 2 分鐘的雙寫者視窗即使 GitOps 之後自癒也值得 page（那段視窗可能已污染 git，且有人手動 scale 單寫者服務本身是該浮現的流程問題，不是該壓掉的 noise）。KSM 無 `--namespaces`/metric-denylist 過濾，兩 gauge 對 `tenant-api` namespace 皆有 emit（KSM 本身掛掉是 KSM-absence 告警的事）。
- **同時是 #787 的 codify re-trigger**：此告警 fire 即「執行期多寫者已成真實風險」→ reopen ADR-023 Deferred A3 實作 leader-election（**勿**抬高 replicaCount）。與 §5.6 `TenantApiReadHANeeded`（A4 的 codify re-trigger）對稱：把延後決策的觸發條件變成會自己叫的訊號，而非關掉的 issue 裡沒人看的 TODO。行為契約：`tests/rulepacks/platform-single-writer-breach_test.yaml`（5 場景：spec 2-副本持續觸發+聚合剝離 label／單副本不觸發／sub-`for` self-heal 瞬態不觸發／3-副本 HPA 向量觸發／**spec=1 但 status=2 的背離經 status 腿觸發**）。

### 5.8 SSE 不作 state 真相來源 — reconnect 必 re-fetch（前端契約，#674）

SSE Hub 廣播是 **at-most-once**：per-client buffer 16（`internal/ws/hub.go:172`），`Broadcast` 對滿 buffer 的 client 直接斷線（`hub.go:141-161`），**無 event replay、無 `Last-Event-ID`、無序號**。client 斷線重連空窗期間的廣播（含 §5.6 部署 `server_shutdown` 重連、網路抖動重連）**永久遺失** —— 重連後是新 channel，錯過的事件不會補送。

**為何 by-design 可容忍**：`onWrite` 在 git commit **成功後**才廣播（`cmd/server/main.go:228-236`），故 SSE 本就**不是寫入結果的真實來源**，只是延遲優化。Save 成功與否的真相是 **HTTP 回應 + 後續 GET**，不是有沒有收到廣播。

**前端契約（綁定 Portal 前端）**：
- SSE 僅作**延遲通知**，**不**作 state 的 source of truth。
- **任何** reconnect（`server_shutdown` 重連、網路斷線重連、分頁喚醒）後，前端**必須主動** `GET /api/v1/tenants/<id>` 拉 authoritative state，**不得**把重連空窗錯過的廣播當資料遺失、亦不得卡在「儲存中…」spinner 等一則可能永不到達的廣播。
- Save 的成敗以該請求的 HTTP 回應為準；UI 一致性以 reconnect 後的 GET 為準。

**重連韌性（契約硬條款，外部對抗 review 補強）**：
- **Exponential backoff + jitter（防 thundering herd）**：reconnect **與其後的 `GET`** 一律帶退避加抖動（`delay = base·2^retry + random(0,1s)`），**不得**斷線瞬間 `reconnect()`。deploy 場景已由 §5.6 的 `server_shutdown` → `reconnect_delay_ms` + client jitter hint 覆蓋；本條把同一紀律推及**所有**重連成因——**網路斷線 / 分頁喚醒收不到 server hint，且 EventSource 原生重連是固定間隔、非退避**。否則一次 rollout 會讓 N 個分頁同毫秒重連 + `GET`、自我 DDoS。
- **Heartbeat watchdog（防 TCP half-open 假死）**：§5.3（#143）後端已每 `TA_SSE_HEARTBEAT`（預設 25s）送 `: keepalive`。前端**必須**據此跑 watchdog timer：逾 ~2× heartbeat（建議 60s，容一次漏接 + §5.6 proxy buffering）未收到**任何** SSE 訊息（含 keepalive）即主動視為斷線、`.close()` 後走重連 + refetch。否則 half-open 時 EventSource `readyState` 仍為 `OPEN` → 前端「自以為連著」永不重連 → 永久 stale data，繞過整個契約。

**未來優化（opt-in，⚠️ 非零後端成本）**：在 SSE event payload 帶 **revision id（git commit SHA）**，前端 `if event.rev > local.rev` 才 fetch、並抑制剛 `PUT` 完的 self-echo（`onWrite` 在 commit 成功後、HTTP 200 返回**前**廣播，故剛存檔的 client 會收到自己的 echo 而多打一次 `GET`），可再砍冗餘 re-fetch。**需後端**在 `onWrite` event（現為 `Type/TenantID/TaskID/Timestamp/Detail`）加 SHA 欄位——逾越本節「零後端成本」定性，故列為對接時優化、**不**入 MVP 契約。

**業界對齊**：SSE 規格上即 at-most-once（可靠重送須 `Last-Event-ID` + server-side event buffer，本服務刻意不實作）；thin-notification + re-fetch 是 webhook 正典（Stripe / GitHub：事件僅提示、狀態回 API 重抓、勿信 payload 為真相）。補完 §5.6 已註的「前端 jitter 重連屬 Portal 對接 future work」—— 本節即該前端契約的內容。

**Scope（#674 分半）**：本節是 #674 的**第 1 半（前端契約，零後端成本、correct-by-design）**。**第 2 半**後端 replay（`Last-Event-ID` / 序號 / event buffer）維持 defer；⚠️ 本契約一旦落地，第 2 半 trigger 即**結構性不可達**（at-most-once 遺失不再有業務後果）→ 屆時轉 close-with-reopen-trigger。對齊 §5.6 `TenantApiReadHANeeded` / §5.7 `TenantApiSingleWriterBreach` 的 codify-trigger 紀律。**狀態**：契約已訂；前端落地隨 Portal 對接（目前無 SSE consumer）。

---

## 6. 相關文件 + 程式碼

- 中介層實作：`components/tenant-api/internal/handler/middleware.go`
- 授權 helper：`components/tenant-api/internal/handler/authz.go`
- 租戶 ID 驗證（pre-existing）：`components/tenant-api/internal/handler/sanitize.go`
- RBAC 核心：`components/tenant-api/internal/rbac/`（v2.5.0 起）
- ADR-009：oauth2-proxy sidecar 整合
- 測試：`components/tenant-api/internal/handler/middleware_test.go`（15 cases，middleware bundle）+ `authz_test.go`（14 cases，tenant-scoped authz）
- v2.7.0：Tenant API basic — 提供 RBAC 框架，本次硬化是其補完
- **跨租戶 ConfigMap 硬化（cluster-plane，分層互補）**：本文是 tenant-api 的 **app-plane** 硬化（`_rbac.yaml` API 授權）；叢集層的 **operator RBAC 收斂 + off-cluster 稽核**基線見 [跨租戶 ConfigMap 硬化](../cross-tenant-configmap-hardening.md)（#926）——兩者是不同 RBAC 層，勿混淆
