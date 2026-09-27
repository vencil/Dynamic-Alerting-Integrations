---
section: Fixed
topic: cli-docs-accuracy
issues: [1380]
created: 2026-09-27T02:38:42+00:00
---
- **動手實驗的練習 2 照抄就能通過驗證，練習 8 的 domain policy 真的會生效（docs；[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）**：練習 2 的五個租戶片段補上 init 產出的 `tenants:` → `<租戶名稱>:` 外框（原本整份貼上會 `0 tenant(s)` 卻顯示 PASS）；新增 `conf.d/_routing_profiles.yaml`（`team-sre-apac`、`domain-finance-tier1`）與 `conf.d/_domain_policy.yaml`（`finance`），並拿掉租戶檔裡不會生效的 `_domain_policy: finance`。練習 3 的預期輸出改為實跑結果（8 個檔、6 項全 PASS），練習 1 的檔案清單補齊 init 實際產出的 13 個檔，練習 8 註明片段放在哪一層。
