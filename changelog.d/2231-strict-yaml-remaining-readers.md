---
section: Fixed
topic: confd-family
issues: [2231]
created: 2026-09-28T09:45:12+08:00
---
- **conf.d 重複鍵：其餘讀者也改為嚴格讀取（tools；[#2231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2231)）**：`config_diff`（`_profiles.yaml`）、`diagnose --show-inheritance`、`backtest_threshold`、`generate_tenant_mapping_rules --validate`、`analyze_rule_pack_gaps --tenant-config`、`migrate_conf_d`、`generate_tenant_metadata` 遇到同一 mapping 寫兩次同一 key 時，不再取最後一個值算出 exporter 不會套用的差異、繼承鏈或清單，而是走各自對語法錯的既有路徑（相同 exit code；`diagnose` 與 `generate_tenant_metadata` 印 WARN 並略過該檔）。`threshold_govern` 的 PUT 前驗證也拒收這種檔，不會再送出。`migrate_conf_d` 遇到語法錯改為 exit 2 並指出檔案，原本是 traceback exit 1。
