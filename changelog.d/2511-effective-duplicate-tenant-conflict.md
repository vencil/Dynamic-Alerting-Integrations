---
section: Fixed
topic: tenant-api
issues: [2511]
created: 2026-10-03T14:45:28+00:00
---
- **同一租戶重複宣告時 `/effective` 改回 409，且不再洩漏伺服器路徑（tenant-api；[#2511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2511)）**：conf.d 頂層的 `<id>.yaml` 與 `<id>.yml` 都宣告同一租戶時，`GET /api/v1/tenants/{id}/effective` 過去回 `500 INTERNAL_ERROR`，訊息帶兩個檔的伺服器絕對路徑。現在回 `409 CONFLICT`，訊息只帶兩個基底檔名。其他重複（子目錄檔，含子目錄內兩種拼法並存；共用檔的 `tenants:`）也回 `409 CONFLICT`，訊息是固定文字、不含檔名。另一個拼法沒有宣告該租戶（`tenants: {}`、只宣告別的租戶、解析不了、壞掉的 symlink）時照常回 200。其他內部錯誤（例如 conf.d 根目錄不存在）改回固定訊息，不帶路徑；完整錯誤只寫進伺服器 log。OpenAPI 補上 `/effective` 的 409。
