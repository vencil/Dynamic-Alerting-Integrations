---
section: Fixed
topic: da-tools
issues: [2995]
created: 2026-10-14T23:39:12+00:00
---
- **`test-notification` 遇到格式錯誤或被截斷的回應不再整支崩潰（[#2995](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2995)）**：receiver 回了不合格的狀態行（`BadStatusLine`）或 body 短於 `Content-Length`（`IncompleteRead`）時，先前沒有任何錯誤處理接住，工具 traceback 退出（結束碼 `1`），其他 receiver 的結果一起丟失。現在該 receiver 標成 `HTTP_ERROR`，detail 以 `malformed response:` 開頭並寫出錯誤型別，其餘 receiver 照常測試。連線重置、DNS、TLS 錯誤仍標成 `CONNECTION_REFUSED`，另案處理。
