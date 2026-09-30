---
section: Fixed
topic: tenant-api
issues: [2477]
created: 2026-09-30T12:14:53+00:00
---
- **租戶檔不是一般檔案時，讀寫改為立即回明確錯誤（tenant-api；[#2477](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2477)）**：conf.d 裡某個租戶的檔案不是一般檔案（例如具名管道、裝置檔、socket）時，`GET /api/v1/tenants/{id}`、整檔 `PUT /api/v1/tenants/{id}`、`PUT /api/v1/tenants/{id}/custom-alerts` 先前可能無法完成。現在這三支立即回 409 `TENANT_CONFIG_NOT_LOADABLE`，`config_error` 為 `not_regular_file`，不讀也不寫該檔；寫入權限檢查讀不到該檔時照舊當成未標記租戶。`config_error` 值變更：`GET /api/v1/tenants` 與 `/search` 對開檔即失敗、但目標存在且不是一般檔的項目（例如 socket、指向 socket 的 symlink、tenant-api 沒有讀取權限的非一般檔），由 `unreadable` 改為 `not_regular_file`，與上述三支同一個值。修復方式是在 git 裡把它換回一般檔案。若該項目讀取時會一直等待或讀不完，換回之前其他租戶的寫入會在 conf.d 掃描上限後回 500（不寫任何東西）；若換回之前已有寫入因掃描逾時而失敗，換回後需重新啟動 tenant-api 寫入才會恢復。
