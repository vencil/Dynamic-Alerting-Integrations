---
section: Fixed
topic: tenant-api
issues: [2369]
created: 2026-09-29T00:49:03+08:00
---
- **`GET /api/v1/tenants/{id}` 的 `resolved_thresholds` 改依根目錄 `_defaults.yaml` 的 `max_metrics_per_tenant` 截斷（tenant-api；[#2369](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2369)）**：先前 GET 的合併核心沒有帶入這個設定，一律以內建的 500 截斷。上限設得比 key 數小時，GET 會列出 `/metrics` 已截掉的閾值；上限調到超過 500 時，GET 反而少列。現在與 exporter、da-guard 讀同一個值，只認根目錄的 defaults carrier。這個上限只影響讀取，寫入驗證接受與拒絕的內容不變。
