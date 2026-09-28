---
section: Fixed
topic: alertmanager-routing
issues: [2315]
created: 2026-09-28T12:42:05+00:00
---
- **`generate-routes` 擋下同一租戶寫在兩個檔（alertmanager-routing；[#2315](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2315)）**：同一個 tenant id 出現在兩個租戶檔時，exporter 與 da-guard 會拒收整棵 conf.d，產生器以前卻把兩份合併、結束碼 0。現在所有模式（`--validate`、預設 render、`--output-configmap`，不分 `--strict`）都結束碼 1、不寫檔，訊息點名每個檔（含子目錄裡的）；validate-config 的 `schema` 列經同一個判定一起 FAIL。`_` 平台檔的 `tenants.<id>` overlay 不算重複；exporter 無法完整 decode、整份略過的檔也不算宣告。CI 的必要檢查 `Validate Tenant Config & Routes` 另對 `try-local/seed/conf.d/` 跑一次，seed 的 `db-demo` 加了最小 `_routing`；租戶檔的 routing 由這一步負責，da-guard 只在 `_` 平台檔變更時觸發。
