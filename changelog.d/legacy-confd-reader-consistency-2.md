---
section: Fixed
topic: confd-reader-consistency
issues: [1339, 1577, 1588, 1603, 1605, 1670, 1673, 1674, 1676, 1791, 1792, 1827, 1911, 1964]
created: 2026-09-26T17:00:00+00:00
---
- **寫入與部署路徑不再漏掉租戶或造出雙份宣告（tenant-api、exporter、ops；[#1673](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1673)、[#1674](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1674)、[#1605](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1605)）**：`make configmap-assemble` 現在也收 `.yml` 租戶，並在 `kubectl apply` 前擋下同一租戶被兩個檔宣告的樹（exporter 會因此拒載整棵樹）；大寫副檔名仍不收，但會逐檔列出。tenant-api 對 `.yml` 租戶的讀寫落在同一個實際檔案，不再旁建 `.yaml`；同 stem 兩種拼法並存時回 **409**，⚠️ `GET /tenants` 對這類樹改為 500 並指名兩個檔。`da-batchpr` 的檔案分桶對齊 exporter 規則，不再丟掉或誤路由載體。⚠️ 同一目錄有多個 `_defaults` 拼法時只讀一個（`.yaml` 優先）並 WARN，非載體 `_` 檔的 `defaults:` 不再進 `/metrics`，這類樹的 `merged_hash` 與 effective config 會變（出貨的 conf.d 沒有此類檔案）。`assemble_config_dir` 不再組入 exporter 不讀的隱藏檔。
