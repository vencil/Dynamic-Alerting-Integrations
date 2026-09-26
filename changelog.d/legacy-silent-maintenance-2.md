---
section: Changed
topic: silent-maintenance
issues: [1988]
created: 2026-09-26T17:00:00+00:00
---
- **靜音／維護狀態只剩一份判讀，租戶列表帶出依設定推算的狀態（exporter、tenant-api、portal）**：`pkg/config` 新增 `LoadDir` 與 `OperationalStatesAt`，exporter 與 tenant-api 共用同一份計算，`IsMaintenanceActive` 移除，`LoadDir` 另回傳解析失敗的檔案；exporter 輸出的 metric 不變。`GET /api/v1/tenants` 與 `/tenants/search` 的每個租戶新增 `config_derived`（`silent_targets`／`maintenance_active`），`/tenants/search` 另回 `config_derivation`（含解析失敗的檔名）；原有 `silent_mode`／`maintenance` 在無法推算時改為清空；Tenant Manager 改讀此欄位並標示「依設定推算」，缺值顯示 `unknown`（[#1988](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1988)）。
