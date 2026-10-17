---
section: Fixed
topic: tenant-api
issues: [2520]
created: 2026-10-17T14:05:00+00:00
---
- **tenant-api `GET /api/v1/events` 經正式路由不再一律回 500**：請求計數 middleware 包住 response writer 時沒有轉交 `Flush`，SSE 端點因此判定「不支援串流」，對每個訂閱者回 500 `streaming not supported`。現在轉交 `Flush` 與 `Unwrap`，事件流與 #143 的逐次寫入期限都會到達連線。修好之後浮現的第二個問題一併修正：全域 30 秒請求逾時（chi `Timeout`）也套在這條長連線上，事件流會在 30 秒被切斷、之後再寫入 504；現在只有 `GET /api/v1/events` 不受請求逾時限制（認證、限流、計數與請求紀錄照舊），由 hub 的 heartbeat、逐次寫入期限與可選的連線壽命上限收尾，其他路由照舊 30 秒。
