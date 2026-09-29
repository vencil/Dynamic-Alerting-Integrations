---
section: Fixed
topic: tenant-api
issues: [2373]
created: 2026-09-28T17:11:26+00:00
---
- **exporter 整份跳過的租戶檔，tenant-api 不再當正常租戶列出（tenant-api；[#2373](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2373)）**：宣告了非 UTF-8 租戶 id 的租戶檔，exporter、`/effective` 與 da-guard 都整份跳過，`GET /api/v1/tenants` 與 `/search` 卻仍列為健康列、`GET /api/v1/tenants/{id}` 還回出閾值。現在列表對它回降級列 `config_error: invalid_config`（只有對 environments 與 domains 都不設限的呼叫者看得到）。**行為變更**：`GET /api/v1/tenants/{id}` 遇到無法載入為租戶設定的檔案時回 200，只帶 `raw_yaml`、`source_hash` 與和列表同義的 `config_error`（`malformed_yaml` / `invalid_config`），不帶 `resolved_thresholds`、`custom_alerts` 與 validation 欄位；語法錯誤的檔過去回 500。對這種檔的部分更新（`PUT .../custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`）一律拒絕、不寫入（409 或該筆結果，code 皆為 `TENANT_CONFIG_NOT_LOADABLE`），須先修好租戶檔本身，整檔 `PUT /api/v1/tenants/{id}` 可整份覆蓋（解析不了的檔需要所有租戶的寫入權限，見 [#2405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2405)）。
