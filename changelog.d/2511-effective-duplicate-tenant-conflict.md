---
section: Fixed
topic: tenant-api
issues: [2511]
created: 2026-10-03T14:45:28+00:00
---
- **同一租戶重複宣告時 `/effective` 改回 409，且不再洩漏伺服器路徑（tenant-api；[#2511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2511)）**：conf.d 同時有 `<id>.yaml` 與 `<id>.yml` 時，`GET /api/v1/tenants/{id}/effective` 過去回 `500 INTERNAL_ERROR`，訊息帶兩個檔的伺服器絕對路徑，只有讀取權限的呼叫者就看得到部署的目錄結構。現在與 `GET /api/v1/tenants/{id}` 相同，回 `409 CONFLICT`，訊息只帶相對 conf.d 的檔名。同一 id 由子目錄檔或其他檔的 `tenants:` 再宣告一次時，`/effective` 也回 `409 CONFLICT`，訊息是固定文字、不含任何檔名，完整路徑只寫進伺服器 log。OpenAPI 補上 `/effective` 的 409。
