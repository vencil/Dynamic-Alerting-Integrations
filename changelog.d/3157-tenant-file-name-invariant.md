---
section: Changed
topic: confd-family
issues: [3157]
created: 2026-10-20T16:18:24+00:00
---
- **租戶檔名必須等於宣告的租戶 id（da-tools；[#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157)）**：`da-tools validate-config` 新增 `tenant_file_name` 列：conf.d 租戶檔（整棵樹，`_`／`.` 開頭的檔除外）宣告的租戶 id 與檔名不同、或一個檔宣告多個租戶時 FAIL、結束碼 1，訊息寫出宣告的 id 與該改成的檔名。tenant-api 以檔名當 id、exporter 以 `tenants:` 當 id，兩者不同時 RBAC、稽核與 portal 連結對不上 metric。出貨範例同步改名：`conf.d/examples/{es-prod,mongo-prod,redis-prod}.yaml`、`rule-packs/recipes/examples/conf.d/shop-a.yaml`、`.../finance/pay-a.yaml`。⚠️ breaking：檔名與 id 不同的既有樹 validate-config 會轉紅，請把檔案改名為 `<id>.yaml`。
