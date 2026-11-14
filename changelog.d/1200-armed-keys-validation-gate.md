---
section: Changed
topic: rule-packs
issues: [1200]
created: 2026-11-14T13:43:01+00:00
---
- **預設出貨的閾值都登記了依據，並各綁一支 promtool 測試（threshold registry；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：registry 的每個 `arming: default` key 現在必須有 `validation`：`vendor`（一個來源，值相同）或 `consensus`（至少兩個不同組織的來源，出貨值落在它們之間），每個來源附網址與原文。已出貨的 7 個 key 都補上了；值與規則沒有變。另外每個 key 綁一支 `tests/rulepacks/armed-<key>_test.yaml`：載入實際的 rule pack，同一個 tenant 在門檻下方不響、越過後先 pending、過了 `for:` 才響，門檻就是 registry 的值。語料量到會誤判的值（`value_counterexample`）不得預設出貨。生成的 Helm values 與 rule pack 檔頭裡「未啟用」的說明改寫：不再提 shadow 報表，改成實際的出貨條件；DB2 的 `db2_log_usage_percent`、`db2_tablespace_used_percent` 改標「可攜，僅宣告」，因為它們在 `optional_overrides` 層，要先升層才能出貨。
