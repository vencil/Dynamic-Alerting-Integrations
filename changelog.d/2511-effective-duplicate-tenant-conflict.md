---
section: Fixed
topic: tenant-api
issues: [2511]
created: 2026-10-03T14:45:28+00:00
---
- **同一租戶重複宣告時 `/effective` 改回 409，且不再洩漏伺服器路徑（tenant-api；[#2511](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2511)）**：過去 `GET /api/v1/tenants/{id}/effective` 遇到重複宣告回 `500 INTERNAL_ERROR`，訊息帶宣告檔的伺服器絕對路徑。現在回 `409 CONFLICT`，訊息依 walker 回報的前兩個宣告檔決定：都是頂層的 `<id>.yaml`／`<id>.yml` 時只帶這兩個基底檔名，其他（子目錄檔，含子目錄內兩種拼法；共用檔的 `tenants:`）為固定文字、不含檔名；有第三份宣告時訊息可能只列兩個拼法。另一個拼法沒有宣告該租戶（`tenants: {}`、只宣告別的租戶、解析不了、壞掉的 symlink）時照常回 200。其他內部錯誤（例如 conf.d 根目錄不存在）改回固定訊息；檔案解碼失敗仍回解碼器原文，不含路徑。完整錯誤只寫進伺服器 log。OpenAPI 補上 `/effective` 的 409。
