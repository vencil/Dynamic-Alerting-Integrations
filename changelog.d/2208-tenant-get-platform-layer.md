---
section: Fixed
topic: confd-reader-consistency
issues: [2208]
created: 2026-09-28T05:49:07+08:00
---
- **`GET /api/v1/tenants/{id}` 套用根目錄平台檔的 `tenants:` 區塊，閾值與 `/metrics` 一致（tenant-api；[#2208](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2208)）**：過去 `resolved_thresholds` 只套根目錄 defaults，平台替租戶設的值不會出現。現與 `/metrics` 相同：租戶檔逐 key 優先、平台檔不能建立租戶；`_metadata` 照 `/effective` 規則不繼承，`_profile` 仍不展開（#1385）。寫入驗證與 `validation_warnings` 只判租戶檔自己寫的 key；平台檔那一段的問題不擋寫入，改在 notices 以 `platform file <檔名>, entry tenants.<id>:` 回報；租戶的舊拼法 key 被平台檔的新拼法蓋過時也會提示。⚠️ `GET /{id}` 現在會讀所有根目錄 `_*.yaml`，讀不完（例如 FIFO）時回 500，不再卡住；讀取進行中才進來的 GET 共用那一次讀取（看到的是它開始時的內容，最多落後一次讀取的時間），卡住時只佔一條 goroutine；讀取結束後的請求一定重讀。
