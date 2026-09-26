---
section: Changed
topic: tenant-api-write
issues: [678, 787, 788, 1004, 1385, 1559, 1599, 2074, 2078]
created: 2026-09-26T17:00:00+00:00
---
- **tenant-api API 契約與部署設定收緊**：`PUT /api/v1/tenants/{id}` 新增選用的樂觀併發 header `X-DA-Base-Hash`（值為 `GET` 回的 `source_hash`，不符回 409，PR 模式回 501）。⚠️ `--write-mode` 打錯字（含 `DIRECT` 這類大小寫不符）改為拒絕啟動，不再靜默降級成 direct commit；布林環境變數改用 `strconv.ParseBool`，`yes`／`on` 會啟動失敗、`t` 改判為 `true`。錯誤回應統一為帶 `code`／`request_id` 的 envelope，OpenAPI 的 4xx 宣告與實際回應對齊。新增告警 `TenantApiSingleWriterBreach`（副本數大於 1 持續 2 分鐘即 page）與 `TenantApiReadHANeeded`（info）。tenant-api 的 canonical namespace 定為 `tenant-api`，da-portal／recipe-preview 改用 `tenant-api.tenant-api.svc.cluster.local:8080` 並補上對應的 NetworkPolicy。README 說明單一租戶端點與 conf.d 的適用範圍（[#1559](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1559)、[#1599](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1599)、[#1004](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1004)）。
