# recipe-preview — would-fire 預覽服務（#657）

把租戶在 portal 填的一條 recipe，當場回答「會不會觸發」。一支小型 stdlib HTTP 服務，`POST /preview` 走平台**同一套** compiler + `promtool` 評估（不另寫 eval），先上 try-local。

> **誰會讀這份 README？** 是**部署 / 自架**這支服務的人。終端使用者（租戶 / 領域專家）走的是 portal 表單。設計全文見 [recipe-would-fire-preview.md](../../docs/design/recipe-would-fire-preview.md)。

## 端點

| Method | Path | 說明 |
|---|---|---|
| `POST` | `/preview` | `{recipe, tenant, scenario}` → `{alertname, supported, states, warnings}`；`state ∈ firing / inactive / error` |
| `GET` | `/healthz` | `200 {status, promtool, git_sha}`（`promtool` 版本 + image build 的 `GIT_SHA`，供 drift 觀測）|

`POST /preview` request：

```json
{ "recipe": { "...": "ADR-024 recipe（同 portal 表單產出）" }, "tenant": "shop-a", "scenario": { "value": 1500 } }
```

## 安全模型（PEP）

本服務是 **PEP（policy enforcement point）**，**不**自己判租戶授權——把呼叫者身分轉發去打 tenant-api 的讀取探測 `GET /api/v1/tenants/{id}/access`（#876），`200`→放行、`403`／任何非 `200`／連不到→**fail-closed 拒絕**。RBAC 決策留在 tenant-api（單一權威、零跨語言漂移）。

**信任邊界**：與 tenant-api 一樣信任 `X-Forwarded-*` 身分標頭，故**必須**擺在會剝除客戶端標頭、注入認證身分的認證代理（oauth2-proxy）後面，並用 NetworkPolicy 限制 ingress 只能來自該代理（服務**只轉發兩個身分標頭**、不轉發任意客戶端標頭——confused-deputy 防線）。try-local 用 dev-bypass（[ADR-022](../../docs/adr/022-dev-auth-bypass-four-layer-containment.md)）注入 demo 身分。

## 護欄（§6）

並發上限（`PREVIEW_MAX_CONCURRENCY`）、每租戶滑動視窗限流（`PREVIEW_RATE_LIMIT_PER_MIN`，限流器自我 GC 過期 key、key 空間有上限）、request body 上限（`PREVIEW_MAX_BODY_BYTES`，預設 64 KiB，讀進記憶體前先擋）、`promtool` **SHA-pin**（版本見 `Dockerfile` 的 `PROM_VERSION`；firing/inactive 判定的 returncode/輸出格式與版本綁）、啟動時記錄版本。

授權**先於**限流與評估：未授權的呼叫者既無法消耗受害租戶的限流額度、也無法把 key 灌進限流器（pre-auth DoS 防線）。dev-bypass 在偵測到 Kubernetes 環境時**拒絕啟動**（ADR-022 圍堵，防直連 pod 被自動注入 demo 身分）。

## 設定（env）

預設欄寫的是**沒設這個變數時的實際值**；`(空)` = 空字串或未設，意義寫在說明欄。表格由 `tests/preview/test_env_readme_parity.py` 對 `app.py` 比對：變數名單兩向一致、預設值逐一相同。

| 變數 | 預設 | 說明 |
|---|---|---|
| `PREVIEW_TENANT_API_URL` | `http://tenant-api.tenant-api.svc.cluster.local:8080` | PEP 打的 tenant-api base URL（tenant-api 位於專屬 `tenant-api` namespace，#1004）|
| `PREVIEW_AUTHZ_TIMEOUT` | `5` | authz 探測（打 tenant-api `/access`）的 timeout 秒數；逾時→fail-closed 拒絕（`403`）|
| `PREVIEW_AUTH_TOKEN_FILE` | (空) | 送給 tenant-api 的 SA token 檔路徑，只用於對方的 machine-identity 稽核、不影響授權。Helm chart 設成 `/var/run/secrets/tokens/tenant-api-token`。空或讀不到 = 不帶 Bearer、照常預覽（讀不到時印 warning）|
| `PREVIEW_CLAIM_HEADERS` | (空) | 逗號分隔的 header 名稱，有帶就一併轉發給 tenant-api `/access`（例如 org claim，對齊 tenant-api 的 `--identity-claim-headers`）。`X-Forwarded-Email`／`X-Forwarded-Groups`／`Authorization`／`Cookie` 會被忽略並印 warning |
| `PREVIEW_LISTEN_HOST` | `0.0.0.0` | 監聽位址 |
| `PREVIEW_LISTEN_PORT` | `8082` | 監聽埠 |
| `PREVIEW_MAX_CONCURRENCY` | `4` | 同時評估上限（每次評估開一個 `promtool` 子程序）|
| `PREVIEW_QUEUE_TIMEOUT` | `10` | 評估併發 slot 的排隊上限秒數；超時→`503` |
| `PREVIEW_RATE_LIMIT_PER_MIN` | `30` | 每租戶每分鐘上限（`0`=關閉）|
| `PREVIEW_MAX_BODY_BYTES` | `65536` | request body 上限（讀進記憶體前擋；超過回 `413`）|
| `PREVIEW_REQUEST_TIMEOUT` | `60` | 每連線 socket 讀取 timeout 秒數（防 idle／慢速連線占住 thread）|
| `PREVIEW_DEV_BYPASS_AUTH` | (空) | try-local 用：`1`／`true`／`yes`（不分大小寫）開啟，請求沒帶 `X-Forwarded-Email` 時注入下面兩列的 demo 身分；其他值 = 關。偵測到 Kubernetes（`KUBERNETES_SERVICE_HOST` 有值）時開啟會**拒絕啟動** |
| `PREVIEW_DEV_BYPASS_EMAIL` | `dev@local` | dev-bypass 注入的 email |
| `PREVIEW_DEV_BYPASS_GROUPS` | `demo-admins` | dev-bypass 注入的 groups |
| `PREVIEW_CORE_DIR` | (空) | eval core（`_recipe_preview.py` 所在目錄）。image 設成 `/opt/recipe-preview/core/dx`；空或不是目錄就依序試 `app.py` 旁的 `core/dx`、repo 的 `scripts/tools/dx` |
| `GIT_SHA` | `unknown` | 由 image build 的 `GIT_SHA` build arg 寫入、`/healthz` 回報，不必手設 |

## 範圍

支援 `threshold` recipe（`>` `>=` `<` `<=` `==`）+ `absence`（缺口偵測——合成序列**不發該指標**即缺口，`count_over_time(metric[window])` 抓不到樣本 → `unless` 觸發；eval 跨過 window + `for:`）；其餘時間相依型（rate/ratio/forecast/p99）回 `supported:false`（誠實標示、不靜默）。

**預覽答的範圍**：餵的是合成、固定序列，回答的是「這條 recipe 的閾值邏輯在某測試值會不會越線」，**不是**「在你環境會不會發出通知」——不模擬真實數據走勢、`for:` 計時、Alertmanager 靜默／路由（前端 would-fire 面板對使用者明示這條界線）。

**正式環境部署延後**（[設計 §9](../../docs/design/recipe-would-fire-preview.md)）；隨 prod 部署一起補的加固 bundle：

- **`tini` 當 PID 1** — SIGTERM 轉發 + graceful 終止。（非殭屍回收：`subprocess.run` 為同步 `wait()`、已回收 `promtool`，且 `promtool` 不 fork 孫行程，故無 PID-1 殭屍堆積路徑。）
- **生產級 WSGI**（gunicorn／waitress）取代 stdlib `ThreadingHTTPServer` — 完整 worker／timeout／Slowloris 管理。（現以 auth proxy + 每連線 socket timeout 部分緩解。）
- **`/metrics` 端點** — `recipe_preview_requests_total{tenant,result_state}` 與 `recipe_preview_eval_duration_seconds`，把本服務狀態納入平台觀測體系。
- **readiness/liveness 分離（`GET /ready`）** — 對齊 tenant-api 慣例：promtool 缺失 → NotReady。取捨備忘：readiness 不納入 tenant-api 可達性（fail-closed `403` 是正確行為，摘掉輪替只會把它變 connection refused）；promtool-absent gate 需連同 `/metrics` 一起上——否則單副本 NotReady 卻無告警面，比現況「200 + `cannot evaluate` warning」的優雅降級更差。

觸發＝prod 部署，或 Domain Expert 開始實際使用。

## try-local

```bash
cd try-local && docker compose up recipe-preview
curl -s localhost:8082/healthz
```
