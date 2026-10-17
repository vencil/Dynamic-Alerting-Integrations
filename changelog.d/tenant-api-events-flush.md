---
section: Fixed
topic: tenant-api
issues: []
created: 2026-10-17T14:05:00+00:00
---
- **tenant-api `GET /api/v1/events` 經正式路由不再一律回 500**：請求計數 middleware 包住 response writer 時沒有轉交 `Flush`，SSE 端點因此判定「不支援串流」，對每個訂閱者回 500 `streaming not supported`。現在轉交 `Flush` 與 `Unwrap`，事件流與 #143 的逐次寫入期限都會到達連線。
