# recipe-preview — would-fire 預覽服務（#657）

把租戶在 portal 填的一條 recipe，當場回答「會不會觸發」。一支小型 stdlib HTTP 服務，`POST /preview` 走平台**同一套** compiler + `promtool` 評估（不另寫 eval），先上 try-local。

> **誰會讀這份 README？** 是**部署 / 自架**這支服務的人。終端使用者（租戶 / 領域專家）走的是 portal 表單。設計全文見 [recipe-would-fire-preview.md](../../docs/design/recipe-would-fire-preview.md)。

## 端點

| Method | Path | 說明 |
|---|---|---|
| `POST` | `/preview` | `{recipe, tenant, scenario}` → `{alertname, supported, states, warnings}`；`state ∈ firing / inactive / error` |
| `GET` | `/healthz` | `200 {status, promtool}`（含 `promtool` 版本）|

`POST /preview` request：

```json
{ "recipe": { "...": "ADR-024 recipe（同 portal 表單產出）" }, "tenant": "shop-a", "scenario": { "value": 1500 } }
```

## 安全模型（PEP）

本服務是 **PEP（policy enforcement point）**，**不**自己判租戶授權——把呼叫者身分轉發去打 tenant-api 的讀取探測 `GET /api/v1/tenants/{id}/access`（#876），`200`→放行、`403`／任何非 `200`／連不到→**fail-closed 拒絕**。RBAC 決策留在 tenant-api（單一權威、零跨語言漂移）。

**信任邊界**：與 tenant-api 一樣信任 `X-Forwarded-*` 身分標頭，故**必須**擺在會剝除客戶端標頭、注入認證身分的認證代理（oauth2-proxy）後面，並用 NetworkPolicy 限制 ingress 只能來自該代理（服務**只轉發兩個身分標頭**、不轉發任意客戶端標頭——confused-deputy 防線）。try-local 用 dev-bypass（[ADR-022](../../docs/adr/022-dev-auth-bypass-four-layer-containment.md)）注入 demo 身分。

## 護欄（§6）

並發上限（`PREVIEW_MAX_CONCURRENCY`）、每租戶滑動視窗限流（`PREVIEW_RATE_LIMIT_PER_MIN`，限流器自我 GC 過期 key、key 空間有上限）、request body 上限（`PREVIEW_MAX_BODY_BYTES`，預設 64 KiB，讀進記憶體前先擋）、`promtool` **2.53.2 SHA-pin**（firing/inactive 判定的 returncode/輸出格式與版本綁）、啟動時記錄版本。

授權**先於**限流與評估：未授權的呼叫者既無法消耗受害租戶的限流額度、也無法把 key 灌進限流器（pre-auth DoS 防線）。dev-bypass 在偵測到 Kubernetes 環境時**拒絕啟動**（ADR-022 圍堵，防直連 pod 被自動注入 demo 身分）。

## 設定（env）

| 變數 | 預設 | 說明 |
|---|---|---|
| `PREVIEW_TENANT_API_URL` | `http://tenant-api:8080` | PEP 打的 tenant-api base URL |
| `PREVIEW_LISTEN_PORT` | `8082` | 監聽埠 |
| `PREVIEW_MAX_CONCURRENCY` | `4` | 同時評估上限（每次評估開一個 `promtool` 子程序）|
| `PREVIEW_RATE_LIMIT_PER_MIN` | `30` | 每租戶每分鐘上限（`0`=關閉）|
| `PREVIEW_MAX_BODY_BYTES` | `65536` | request body 上限（讀進記憶體前擋；超過回 `413`）|
| `PREVIEW_DEV_BYPASS_AUTH` | `false` | try-local：無身分標頭時注入 demo 身分（k8s 內開會**拒絕啟動**）|

## 範圍

首版 `threshold` recipe（`>` `>=` `<` `<=` `==`）；時間相依型（rate/ratio/forecast/absence）回 `supported:false`（誠實標示、不靜默）。正式環境部署延後（[設計 §9](../../docs/design/recipe-would-fire-preview.md)）。

## try-local

```bash
cd try-local && docker compose up recipe-preview
curl -s localhost:8082/healthz
```
