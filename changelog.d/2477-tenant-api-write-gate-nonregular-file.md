---
section: Fixed
topic: tenant-api
issues: [2477]
created: 2026-09-30T12:14:53+00:00
---
- **租戶檔不是一般檔案時，讀寫改為立即回明確錯誤（tenant-api；[#2477](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2477)）**：conf.d 裡某個租戶的檔案不是一般檔案（例如具名管道、裝置檔）時，`GET /api/v1/tenants/{id}`、整檔 `PUT /api/v1/tenants/{id}`、`PUT /api/v1/tenants/{id}/custom-alerts` 先前無法完成。現在這三支立即回 409 `TENANT_CONFIG_NOT_LOADABLE`，`config_error` 為 `not_regular_file`（與租戶列表同一個值），訊息指明原因，不讀也不寫該檔；寫入權限檢查讀不到該檔時照舊當成未標記租戶。修復方式是在 git 裡把它換回一般檔案；在那之前，其他租戶的寫入照既有設計在 conf.d 掃描上限後拒絕寫入（500，不寫任何東西）。
