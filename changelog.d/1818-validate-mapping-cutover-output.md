---
section: Fixed
topic: cli-docs-accuracy
issues: [1818, 1196]
created: 2026-09-27T16:34:47+00:00
---
- **validate 的 mapping 格式、cutover 預期輸出與幾處範例改成工具的實際行為（docs；[#1818](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1818)）**：cli-reference、da-tools README 與 `da-tools` 的 usage 範例都教 `validate --mapping mapping.csv`。工具只讀 `migrate` 產生的 `prefix-mapping.yaml`，餵 CSV 會 traceback。這幾處改成 YAML，validate 的結束碼表也改成實際語意，並註明 v2.9.0 映像只有參數錯誤會回 2。shadow-monitoring-sop §7.1 的 cutover 預期輸出改成實跑結果，並寫明健康驗證只查 `user_threshold`，不跑 `check-alert`／`diagnose`。其餘三處：速查表 `lint` 說明改成 deny-list 檢查；custom-rule-governance 三態範例的 `mariadb_replication_lag` 改成 rule pack 真的在讀的 `mysql_replication_lag`（[#1196](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1196)）；README 的 `migrate --triage` 產出註解改成實際的 `triage-report.csv`。
