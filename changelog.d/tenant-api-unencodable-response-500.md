---
section: Fixed
topic: tenant-api
issues: []
created: 2026-09-28T22:24:30+00:00
---
- **tenant-api 回應 encode 失敗時改回 500 錯誤信封，不再是 200 空 body（`internal/handler` `writeJSON`）**：先前回應直接 encode 到連線上，status 先送出；值 encode 失敗時錯誤被吞掉，客戶端拿到 `200` 加空 body、log 也沒有紀錄。現在先 encode 到緩衝區，失敗就回 `500`、`code: INTERNAL_ERROR`，錯誤訊息寫出 encode 錯誤並記 log；能 encode 的回應位元組不變。
