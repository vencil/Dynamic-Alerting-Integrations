# recipe-preview — would-fire 預覽服務（#657）

把租戶在 portal 填的一條 recipe，當場回答「會不會觸發」。一支小型 stdlib HTTP 服務，走平台**同一套** compiler + `promtool` 評估（不另寫 eval）。

> **誰會讀這份 README？** 是**部署 / 維運**這支服務的人。終端使用者（租戶 / 領域專家）走的是 portal 表單，portal 呼叫的是 tenant-api 的 `POST /api/v1/tenants/{id}/recipe-preview`。設計全文見 [recipe-would-fire-preview.md](../../docs/design/recipe-would-fire-preview.md)。

## 位置與信任（[ADR-027 §5](../../docs/adr/027-tenant-api-caller-identity.md)）

本服務是 tenant-api pod 裡的一個容器，**只**在兩個容器共用的 volume 裡的 Unix socket 上服務，比照產生器容器（[ADR-036 §3](../../docs/adr/036-single-parser-effective-config.md)）：

- socket 權限 `0600`，每條連線以 `SO_PEERCRED` 檢查對方 uid 必須與本程序相同（tenant-api 以同一 uid 執行）；其他程序的連線不回應直接關閉。
- **沒有網路埠**。tenant-api 先依呼叫者身分授權（對該租戶的讀取權限）才呼叫本服務；本服務**不讀任何身分 header、不向任何人查授權**。

## 端點（socket 上）

| Method | Path | 說明 |
|---|---|---|
| `POST` | `/v1/preview` | `{recipe, tenant, scenario, deadline_ms}` → `{alertname, supported, states, warnings}`；`state ∈ firing / inactive / error` |
| `GET` | `/v1/status` | `200 {status, promtool, git_sha}`（`promtool` 版本 + image build 的 `GIT_SHA`，供 drift 觀測）|

```json
{ "recipe": { "...": "ADR-024 recipe（同 portal 表單產出）" }, "tenant": "shop-a", "scenario": { "value": 1500 }, "deadline_ms": 25000 }
```

## 期限與佇列

`deadline_ms` 是呼叫端（tenant-api）這次請求還剩的時間，必填（`1`..`600000` 的整數）。

- 等評估 slot 最多 `min(PREVIEW_QUEUE_TIMEOUT, 剩餘時間)`：先到 `PREVIEW_QUEUE_TIMEOUT` → `503` `reason: busy`（稍後重試）；先到期限 → `503` `reason: deadline`。
- 評估拿到絕對期限，每次 da-guard／`promtool` 執行的 timeout 不超過剩餘時間。
- 評估結束時已過期限 → `503` `reason: deadline`，不把結果當 verdict 回（被期限截斷的執行讀起來是 `error`，不能當成「不會觸發」）。

## 護欄（§6）

並發上限（`PREVIEW_MAX_CONCURRENCY`）、每租戶滑動視窗限流（`PREVIEW_RATE_LIMIT_PER_MIN`，限流器自我 GC 過期 key、key 空間有上限）、request body 上限（`PREVIEW_MAX_BODY_BYTES`，預設 64 KiB，讀進記憶體前先擋）、`promtool` **SHA-pin**（版本見 `Dockerfile` 的 `PROM_VERSION`；firing/inactive 判定的 returncode/輸出格式與版本綁）、啟動時記錄版本。

## 設定（env）

預設欄寫的是**沒設這個變數時的實際值**；`(空)` = 空字串或未設，意義寫在說明欄。表格由 `tests/preview/test_env_readme_parity.py` 對 `app.py` 比對：變數名單兩向一致、預設值逐一相同。

| 變數 | 預設 | 說明 |
|---|---|---|
| `PREVIEW_SOCKET` | (空) | 監聽的 Unix socket 路徑（與 tenant-api 共用的 volume 裡）。必填：空則拒絕啟動 |
| `PREVIEW_MAX_CONCURRENCY` | `4` | 同時評估上限（每次評估開一個 `promtool` 子程序）|
| `PREVIEW_QUEUE_TIMEOUT` | `10` | 評估併發 slot 的排隊上限秒數（與呼叫端期限取小）；超時→`503` `busy` |
| `PREVIEW_RATE_LIMIT_PER_MIN` | `30` | 每租戶每分鐘上限（`0`=關閉）|
| `PREVIEW_MAX_BODY_BYTES` | `65536` | request body 上限（讀進記憶體前擋；超過回 `413`）|
| `PREVIEW_REQUEST_TIMEOUT` | `60` | 每連線 socket 讀取 timeout 秒數（防 idle／慢速連線占住 thread）|
| `PREVIEW_CORE_DIR` | (空) | eval core（`_recipe_preview.py` 所在目錄）。image 設成 `/opt/recipe-preview/core/dx`；空或不是目錄就依序試 `app.py` 旁的 `core/dx`、repo 的 `scripts/tools/dx` |
| `GIT_SHA` | `unknown` | 由 image build 的 `GIT_SHA` build arg 寫入、`/v1/status` 回報，不必手設 |

## 範圍

支援 `threshold` recipe（`>` `>=` `<` `<=` `==`）+ `absence`（缺口偵測——合成序列**不發該指標**即缺口，`count_over_time(metric[window])` 抓不到樣本 → `unless` 觸發；eval 跨過 window + `for:`）；其餘時間相依型（rate/ratio/forecast/p99）回 `supported:false`（誠實標示、不靜默）。

**預覽答的範圍**：餵的是合成、固定序列，回答的是「這條 recipe 的閾值邏輯在某測試值會不會越線」，**不是**「在你環境會不會發出通知」——不模擬真實數據走勢、`for:` 計時、Alertmanager 靜默／路由（前端 would-fire 面板對使用者明示這條界線）。

**正式環境部署延後**（[設計 §9](../../docs/design/recipe-would-fire-preview.md)）；隨 prod 部署一起補的加固 bundle：

- **`tini` 當 PID 1** — SIGTERM 轉發 + graceful 終止。（非殭屍回收：`subprocess.run` 為同步 `wait()`、已回收 `promtool`，且 `promtool` 不 fork 孫行程，故無 PID-1 殭屍堆積路徑。）
- **生產級 server** 取代 stdlib `ThreadingMixIn` — 完整 worker／timeout 管理。（現只有 tenant-api 能連上 socket，加上每連線 socket timeout。）
- **`/metrics` 端點** — `recipe_preview_requests_total{tenant,result_state}` 與 `recipe_preview_eval_duration_seconds`，把本服務狀態納入平台觀測體系。
- **promtool 缺失的告警面** — promtool-absent gate 需連同 `/metrics` 一起上——否則單副本 NotReady 卻無告警面，比現況「200 + `cannot evaluate` warning」的優雅降級更差。

觸發＝prod 部署，或 Domain Expert 開始實際使用。

## try-local

try-local 的 compose 讓本服務與 tenant-api 共用 socket volume；從 portal 的 recipe 表單呼叫，或經 oauth2-proxy 打 `POST /api/v1/tenants/<id>/recipe-preview`。
