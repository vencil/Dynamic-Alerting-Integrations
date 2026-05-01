---
title: "Tenant API Hardening (v2.8.0)"
date: 2026-04-29
audience: platform-ops, sre, security
verified-at-version: v2.8.0
---

# Tenant API Hardening — v2.8.0 Phase B Track C

> v2.7.0 出貨的 tenant-api 已具備基本 RBAC 與標準 chi 中介層（RequestID / RealIP / Logger / Recoverer / Timeout 30s）。本次 v2.8.0 Phase B Track C 是「客戶導入前的硬化」批次：補上**速率限制**、**X-Request-ID 回應標頭**、**Groups / Views / Task / PR 端點的租戶級授權**三個 production gap。
>
> 配套 PR：PR-1（middleware bundle）+ PR-2（tenant-scoped authz）。

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

---

## 3. Tenant-Scoped Authorization

v2.7.0 的 RBAC 透過 `rbacMgr.Middleware(perm, tenantIDFn)` 在路由層做 `PermRead`/`PermWrite` 檢查；對「path-param 內單一租戶」端點正確，但對**接收租戶清單**或**回應跨租戶資料**的端點留有 information disclosure 漏洞。v2.8.0 Track C PR-2 補完這四類端點的租戶級授權。

### 3.1 受影響端點 + 行為變更

| 端點 | v2.7.0 行為 | v2.8.0 行為 |
|---|---|---|
| `PUT /api/v1/groups/{id}` | 任何 `PermWrite` user 可編輯任意 group 的 `members` | 必須對**每個** member tenant 有 `PermWrite`；缺者列入 403 訊息 |
| `DELETE /api/v1/groups/{id}` | 任何 `PermWrite` user 可刪除任意 group | 必須對 group 既有**每個** member 有 `PermWrite`（防 DoS）|
| `GET /api/v1/tasks/{id}` | 回傳完整 `Results[]`（含所有 task 觸及租戶）| 過濾 `Results[]` 為 caller 可讀的子集；零可讀子集回 403 |
| `GET /api/v1/prs` | 回傳所有 pending PR/MR | bulk 模式：自動過濾不可讀租戶；`?tenant=<id>` 模式：不可讀回**空列表**（不 403，避免 existence oracle）|

### 3.2 為什麼 `?tenant=<id>` 不直接回 403

對 `GET /api/v1/prs?tenant=db-secret` 這類 query，403 會**洩露 db-secret 的存在性**（caller 能看到「我的權限不夠」=「該租戶確實存在」）。空列表則和「該租戶沒有 pending PR」無從區分 — 是 API surface 想要的行為。

> **乍看是 bug-feature，實際是刻意 security UX choice。** 後續重構 PR 若把它「修」成 403，會回退這個 oracle，因此 `TestListPRs_TenantQueryReturnsEmptyWhenForbidden` 鎖死當前行為。

### 3.3 為什麼 Views 不在範圍內

`PutView` / `DeleteView` 也接收 `Filters map[string]string`，看起來像 group 的 members。但 view filters 是**任意 metadata 字串**（例 `severity:critical`、`team:platform`），不是嚴格 tenant ID 列表 — 不能對 filter 內容做 RBAC 檢查（不知道該檢查什麼）。view 真正暴露 tenant 資料的時機是 dashboard 用 view 跑 query，那時 tenant 級 RBAC **本來就**會檢查。

→ Views 留待未來如果加「filter 必須為 tenant ID 列表」型別約束時再補。

### 3.4 為什麼 `_metadata` 路徑推斷不在範圍內

ADR-017 提到「flat tenant 缺 `_metadata.{domain,region,environment}` 時可從父目錄路徑推斷」— 這是**遷移工具**（`migrate_conf_d.py`）的功能，runtime tenant-api 不做。Track C 不擴 RBAC core 行為。

### 3.5 Open-mode RBAC（缺 `_rbac.yaml`）行為

| Permission | Open-mode 行為 |
|---|---|
| `PermRead` | **全 grant**（pre-prod / dev 用，所有 authenticated user 可讀）|
| `PermWrite` | **不 grant**（避免無 RBAC 配置的環境誤允許寫）|
| `PermAdmin` | **不 grant** |

→ Open-mode 環境下，`PutGroup` / `DeleteGroup` 仍會在 PR-2 的新 tenant-scoped check 處被擋下（因 PermWrite 拒）。這是預期行為：production hardening 不該因為操作者忘了部署 `_rbac.yaml` 而退化為「人人可寫」。

### 3.6 錯誤訊息設計

403 訊息**完整列出**所有禁用的 tenant ID — 不只第一個。理由：操作者調修權限時**一次知道所有需要修的**比 retry-and-discover 高效。

```json
{
  "error": "insufficient permission to write group with forbidden member tenants: db-b, db-c"
}
```

去重 + 按請求順序保留：caller 可直接 grep 自己的 RBAC config 找原因。

---

## 4. 升級指引

### 4.1 Production rollout

| 階段 | 動作 | 風險 |
|---|---|---|
| 1. Deploy v2.8.0 | 預設 `TA_RATE_LIMIT_PER_MIN=100`；Groups/Views/Task/PR 開始強制 tenant-scoped authz | 客戶端跑高 QPS（>100/min）會被擋；自動化巡檢腳本可能踩到限流 |
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

未補 → PUT/DELETE Groups 在 PR-2 新 check 處 403。修補本身是 ~5 行 YAML，不擋 v2.7.0 → v2.8.0 升級。

---

## 5. 已知 gap（不在 Track C 範圍）

### 5.1 ~~Body 內容範圍校驗~~（C4 ✅ landed v2.8.x via [issue #134](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/134)）

**Status**：v2.8.x hardening PR 落地。`POST /api/v1/tenants/batch` / `PUT /api/v1/groups/{id}` / `PUT /api/v1/views/{id}` body 已加入 `go-playground/validator` + struct tag + per-key Patch validator registry。

**驗證範圍**：

| 欄位 | 規則 |
|---|---|
| `BatchRequest.operations` | 1-1000 entries |
| `BatchOperation.tenant_id` | required, 1-256 chars |
| `BatchOperation.patch` 一般 key/value | key ≤ 256 chars, value ≤ 1024 chars |
| `BatchOperation.patch._silent_mode` | enum `{warning, critical, all, disable}`（case-insensitive，跟 threshold-exporter resolve 對齊）|
| `BatchOperation.patch._timeout_ms` | integer 0..3,600,000（≤ 1h）|
| `BatchOperation.patch._quench_min` | integer 0..86,400（≤ 1d）|
| `BatchOperation.patch._routing_profile` / `_profile` | 1-256 chars |
| 其他 `_*` 開頭 reserved key | **soft whitelist** — 通過（避免 tenant-api 跟 threshold-exporter release cadence 耦合）|
| `PutGroupRequest.label` / `PutViewRequest.label` | required, 1-256 chars |
| `PutGroupRequest.description` / `PutViewRequest.description` | ≤ 4096 chars |
| `PutGroupRequest.members` | 0-1000 entries, each 1-256 chars |
| `Filters` map values | ≤ 1024 chars per value |

**Failure response shape**：

```json
{
  "error": "validation failed",
  "code": "INVALID_BODY",
  "violations": [
    {"field": "operations[0].patch[\"_timeout_ms\"]", "reason": "must be ≤ 3600000; got 99999999999"},
    {"field": "operations[1].patch[\"_silent_mode\"]", "reason": "must be one of {warning, critical, all, disable}; got \"purple\""}
  ]
}
```

ALL violations 全列出（不是 first-only），跟 PR-2 forbidden-tenant 列表 UX 一致 — 客戶一個 round-trip 就能 fix 所有問題。

### 5.2 Server-level timeout / body-size config 仍 hardcoded

`http.Server{ReadTimeout: 15s, WriteTimeout: 30s, IdleTimeout: 60s}` 與 1MB body limit 都寫死在 code 裡。未來如客戶 ops 需要不同環境的調參，再透過 Helm value 暴露。**Track C 不擴此 surface** — 預設值已在合理範圍。

### 5.3 SSE client idle timeout

`/api/v1/events` SSE endpoint 的 hub 沒有 per-client idle timeout（slow client 會佔 goroutine）。Track C 不處理 — 是另一條 SSE-specific hardening 路徑，與 RBAC / rate limit 解耦。

---

## 6. 相關文件 + 程式碼

- 中介層實作：`components/tenant-api/internal/handler/middleware.go`
- 授權 helper：`components/tenant-api/internal/handler/authz.go`
- 租戶 ID 驗證（pre-existing）：`components/tenant-api/internal/handler/sanitize.go`
- RBAC 核心：`components/tenant-api/internal/rbac/`（v2.5.0 起）
- ADR-009：oauth2-proxy sidecar 整合
- 測試：`components/tenant-api/internal/handler/middleware_test.go`（15 cases，PR-1）+ `authz_test.go`（14 cases，PR-2）
- v2.7.0 B-3：Tenant API basic — 提供 RBAC 框架，本次硬化是其補完
