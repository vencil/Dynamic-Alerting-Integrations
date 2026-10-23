---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T09:42:29+00:00
---
- **`offboard_tenant` 認租戶自己的檔改看檔名帶的租戶 id（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：交叉引用檢查與指標列表原本把任何以 `<租戶>.` 開頭的檔都當成租戶自己的檔；現在只認檔名帶著這個租戶 id 的那一個（da-guard 的 `tenant_named`）。例如 `db-a.bak.yaml` 帶的是 `db-a.bak`，它對 `db-a` 的引用現在會列為交叉引用，`db-a` 的指標也只從 `db-a.yaml` 讀。
