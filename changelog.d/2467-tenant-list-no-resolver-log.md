---
section: Fixed
topic: tenant-api
issues: [2467]
created: 2026-09-30T12:15:00+00:00
---
- **租戶列表與搜尋不再每次請求都把 resolver 的 WARN 寫進 tenant-api log（tenant-api；[#2467](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2467)）**：`GET /api/v1/tenants` 與 `GET /api/v1/tenants/search` 推算每個租戶的 silent mode／maintenance 狀態時，原本會把租戶設定裡無效的 `_silent_mode`、`_state_maintenance`（無法解析、未知值、`expires` 不是 RFC3339）的 WARN 每次請求都寫一遍。推算結果不變，只是不再寫 log。threshold-exporter 抓取時的 log 行為不變；`pkg/config` 新增 `OperationalStatesAtLogf`，由呼叫端傳入 log 函式（`nil` 為不寫）。
