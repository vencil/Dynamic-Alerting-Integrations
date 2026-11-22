---
section: Added
topic: tenant-api
issues: [2789]
created: 2026-11-22T04:21:19+00:00
---
- **租戶自選的日誌可見度子集（tenant-api、helm/vector 0.13.0；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：ADR-021 決定的第二層可見度已實作。租戶經 `GET`／`PUT /api/v1/tenants/{id}/federation/log-visibility`（`PUT` 需該租戶 admin）選擇自己的日誌副本保留哪些欄位，存在 state store 的 `_log_visibility.<id>.yaml`；只能從平台策展的欄位中挑，`tenant_id`、`log_type`、`log_event_id`、`timestamp` 永遠保留，`{"fields": null}` 回到預設（全部策展欄位）。平台把選擇抄進 `tenantProjections[].visibleFields` 後，Vector 在寫入租戶分區時套用，只影響之後寫入的列；不在 `tenantProjectionKeepFields` 的欄位會讓 chart 渲染失敗。沒有任何租戶設定時，Vector 設定與先前逐字相同。新增 values `tenantProjectionAlwaysVisibleFields`。
