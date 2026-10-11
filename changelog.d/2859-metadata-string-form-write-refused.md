---
section: Changed
topic: tenant-api
issues: [2859]
created: 2026-10-11T05:40:00+00:00
---
- **⚠️ 寫入端拒收字串形式的 `_metadata`，exporter 照讀並計數（tenant-api、threshold-exporter；[#2859](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2859)）**：schema 規定 `_metadata` 是 mapping，但 exporter 解碼後，讀得成 YAML 的字串與 mapping 是同一個值，只有寫入當下分得出來。tenant-api 的整檔寫入（`PUT` 等）遇到目標租戶的 `_metadata` 是字串時回 400；`POST /tenants/batch`、`POST /groups/{id}/batch` 的 patch 值一律是字串，所以改為不能寫 `_metadata`，回 400 並指名欄位，請改用 `PUT /api/v1/tenants/{id}` 寫成 mapping。租戶檔把自己的 `_metadata` 寫成字串的租戶，改寫成 mapping 前無法經 tenant-api 修改；profile 或平台檔的字串寫法不擋租戶寫入。conf.d schema lint 本來就拒收這種寫法。exporter 照舊讀，不拒收整個檔；新增 gauge `da_config_metadata_string_form`，計算仍用字串寫法的 `tenants:`／`profiles:` 項目數，每次 commit 重設並以 WARN 點名。
