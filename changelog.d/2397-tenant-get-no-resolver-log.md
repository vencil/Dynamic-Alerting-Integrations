---
section: Fixed
topic: tenant-api
issues: [2397]
created: 2026-09-29T14:16:05+00:00
---
- **GET 租戶設定不再每次請求都把 resolver 的 ERROR/WARN 寫進 tenant-api log（tenant-api；[#2397](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2397)）**：`GET /api/v1/tenants/{id}` 原本每次 resolve 都把該租戶的截斷 ERROR 與各種無效值 WARN 寫進 process log，重複 GET 就重複寫。合併核心（`TenantMerge`）的 resolve 改為不寫 log，回應內容不變；這些行也不另放進 `validation_warnings`（那是會擋寫入的集合，這些不是）。threshold-exporter 抓取時的 log 行為不變。
