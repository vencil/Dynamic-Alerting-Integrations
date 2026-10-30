---
section: Changed
topic: tenant-api
issues: [3016]
created: 2026-10-30T18:00:00+00:00
---
- **⚠️ breaking：tenant-api 依鍵的判決擋下寫入，不再自己判「這個鍵有沒有人讀」（ADR-038 決定 5、§11）**：寫入對象租戶有 error 級的 finding（拼錯的閾值鍵、未登錄的 `_` 鍵、產生器不讀的 `_routing` 鍵等，檔案裡本來就有的也算）時回 400 `BAD_REQUEST`，逐行列出 `key verdict: error: …`；批次為 422 `rejected`，該租戶第一筆有變更的結果帶它。判決未完成時不收：時間不夠回 429 `GENERATOR_BUSY`（`key_verdict_no_time`、`key_verdict_deadline`），其他回 422 `GENERATOR_REJECTED`（`key_verdict_memory`、`key_verdict_error`、`key_verdict_undecidable`、`key_verdict_platform_file`）；沒回判決（舊映像）回 503 `generator_too_old`。寫入路徑的租戶鍵驗證不再回 `unknown key`、`unknown reserved key`、`unknown routing key` 這類錯誤（值的檢查照舊），批次 patch 也不再以 `INVALID_BODY` 擋這些鍵。同一個檔裡、不是這次寫入對象的租戶，它的錯不再擋寫入；值為 null 的未知鍵照收。`validate` 與 `PUT` 預覽不問產生器，看不到這類問題。GET、exporter 與 Python 的鍵名判斷同一版內改用共用判決。
