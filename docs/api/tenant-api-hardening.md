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

---

## 3. Tenant-Scoped Authorization

v2.7.0 的 RBAC 透過 `rbacMgr.Middleware(perm, tenantIDFn)` 在路由層做 `PermRead`/`PermWrite` 檢查；對「path-param 內單一租戶」端點正確，但對**接收租戶清單**或**回應跨租戶資料**的端點留有 information disclosure 漏洞。v2.8.0 tenant-scoped authz 補完這四類端點的租戶級授權。

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

未補 → PUT/DELETE Groups 在新 tenant-scoped check 處 403。修補本身是 ~5 行 YAML，不擋 v2.7.0 → v2.8.0 升級。

---

## 5. 已知 gap（不在本次硬化範圍）

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

### 5.5 PR-mode checkout 紀律 + SIGKILL 殘鎖自癒（#638）

§5.4 的逾時 SIGKILL 衍生的兩處寫入路徑硬化：

- **De-relativize checkout（防跨租戶分支污染）**：`WritePR`/`WritePRBatch` 原本從「當前 HEAD」`checkout -b` 並靠相對 `checkout -` 切回。若工作區被前次寫入留在某 feature branch，下一個租戶會**從別人的 feature branch 分叉**、PR 靜默夾帶他人未推送設定。現在每次 PR 寫入**開頭以 ironclad `reset --hard HEAD` + `checkout -f <base>` 洗白**再 `-b`、所有切回改用同一 clean checkout，污染**不可能發生**（任何 stuck 狀態下次自我矯正）。**為何是 ironclad 而非 plain `checkout`**：寫檔成功但 commit 未完成即被 SIGKILL 會留 dirty tree，plain `checkout <base>` 會被擋（"local changes would be overwritten"）→ wedge 住後續每個 PR 寫入，PVC-backed conf.d 連 pod 重啟都解不開（death-loop）。base 由 `TA_GIT_BASE_BRANCH`（預設 `main`、**forge-neutral**、`--git-base-branch` flag）決定；base 不可達即 abort（不從未知 ref 分叉）。
- **SIGKILL 殘鎖自癒**：被逾時 SIGKILL 的本地 `git add`/`commit` 會留 `.git/index.lock`（及 `HEAD.lock`、`refs/**/*.lock`、`packed-refs.lock`、`config.lock`）；因所有寫入共用 `sync.Mutex`，一個殘鎖會讓後續每個租戶寫入都 `index.lock: File exists` 直到人工介入。`gitErr` 的 deadline 分支現在 best-effort 清掉這些鎖 —— 安全性僅來自 mutex 序列化 + conf.d 由單一 replica 獨占（當下無並發 git 持鎖）。

### 5.6 部署策略 `Recreate` 與 SSE 重連（read-HA trade-off，#677 / #675 / #740）

寫平面是**單寫者**（ADR-023）。為消除滾動更新交疊期的「幽靈副本」多寫者 correctness bug（#677），tenant-api Deployment 採 **`strategy: Recreate`**（殺舊 pod 再起新 pod，無交疊）。**對價**：每次部署都會結束所有開啟中的 `GET /api/v1/events` SSE 連線。

- **優雅關機（#675）**：SIGTERM 時 `Hub.Shutdown` 先廣播一則 `server_shutdown` 控制事件、再關閉所有 SSE 連線，**然後**才 `http.Server.Shutdown`。兩個效果：(1) SSE 連線永不 idle，若不先關，`srv.Shutdown` 會卡滿 15s grace period 才硬切——先關讓 Shutdown 毫秒級完成；(2) client 收到的是**可行動的訊號 + 重連提示**而非生硬的 connection reset。事件契約：`{"type":"server_shutdown","reconnect_delay_ms":2000}`——well-behaved client 應等 `reconnect_delay_ms` **加自身隨機 jitter** 再重連，把重連流量打散離開尚未 ready 的新 pod。Shutdown 一旦開始，在 `srv.Shutdown` 之前抵達的 late request 會被拒（`/api/v1/events` 回 **503**），避免它開一條錯過 hint、又拖住關機的新串流。〔前端 jitter 重連屬 Portal 對接 future work——目前無 SSE consumer，server 先發出契約事件作為基礎。〕
- **預期行為（自癒）**：SSE client 標準會自動重連；hub 已用 heartbeat + per-write deadline 硬化（#143）。**單次部署的重連是預期且自癒的**，讀取中斷僅數秒。`tenant_api_sse_clients` gauge 在重連完成後回升。
- **觀測護欄（#740）**：alert `TenantApiSSEReconnectFailure` 偵測「重連**失敗**」—— 三條件 `for: 10m`（皆對 `tenant_api_sse_clients` / `tenant_api_uptime_seconds` 做 `sum/min without (pod, instance, endpoint)` 聚合，見下）：聚合 client 數 `== 0`（現無連線）且 **`min(uptime_seconds) < 1800`**（pod 近 30m 內**重啟過** —— load-bearing：把告警錨在部署窗，否則無法區分「重連失敗」與「使用者正常關掉 Tenant-Manager 分頁」，低頻 admin UI 會狂誤報）且 `max_over_time(sum(...)[30m:1m]) > 0`（近 30m 有過 client、確有東西該重連）。**為何聚合**：Recreate 換 pod 後若 scrape 帶 per-pod label（如 ServiceMonitor / endpoints role），新 pod 是不同 series、其 30m 歷史全 0，PromQL `and` 需精確 label match → alert 會**靜默永不觸發**。把易變 label 聚合掉（單寫者＝單 pod，安全）讓新舊 pod 合成單一邏輯 series、歷史橫跨重啟，對**任何 scrape role 都正確**。正常單次重連（秒級回升）不誤報；無人連線不誤報；非部署期關分頁（uptime 大）不誤報；client 回連 / uptime 過 30m 即自動 resolve。行為契約由 `tests/rulepacks/platform-sse-reconnect_test.yaml` 的 promtool 測試（4 場景，**含雙-pod 跨重啟**）鎖定。
- **read-HA 是 deferred（#678 / #788，已關閉並 codify re-trigger）**：給讀取 zero-downtime 的正解是讀寫拆分部署（read deployment 多副本 RollingUpdate + binary read-only enforcement、write deployment 維持單副本 Recreate）。**刻意 defer**——成本中等，且「讀取 HA 是需求」這個前提目前**無 field data 驗證**（讀路徑為低頻 admin UI、發版數秒 blip 未經量測證實有害）；在需求成形前先蓋＝服務一個幻影需求。為免 defer 流為「關掉的 issue 裡一句沒人看的 TODO」，trigger 已**codify 成會自己叫的 alert** `TenantApiReadHANeeded`（severity `info`、不 page）：對同一個 `tenant_api_sse_clients` gauge 取 **7d 平均並發 > 2**（`avg_over_time(...[7d:1h])`，取平均非峰值＝只認「持續多人」、不被「單日尖峰」誤觸；另加 `count_over_time(...) >= 150` 守 cold-start，因 `avg_over_time` 只平均「已存在」的樣本，fresh deploy 的 partial <7d 視窗否則會在沒有真正 7d 基線前就誤觸）即「read-HA 成為真實需求」、屆時 reopen ADR-023 Deferred A4 實作讀寫拆分。**⛔ 觸發時注意**：A4 單做只買到**讀** zero-downtime，**寫**路徑（Save）發版仍中斷，需與 A3（K8s Lease，#787）**同排**、勿單獨出 A4。互補可一併看 `rate(tenant_api_requests_total)`（讀為主、寫為極小的 `writes_total`）。行為契約：`tests/rulepacks/platform-read-ha-trigger_test.yaml`。在那之前 Recreate 的數秒讀取 blip 是接受的對價。

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
