---
section: Fixed
topic: alertmanager-routing
issues: [3918]
created: 2026-11-15T09:58:56+00:00
---
- **路由產生器、validate-config、retire-drift 檢查與 describe-tenant 的結論不再隨執行時刻改變（route generator、validate-config、describe-tenant、da-guard；[#3918](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3918)）**：custom-alert 送達檢查、`check_retire_drift` 與 describe-tenant 的 `custom_alerts` 先前讀 served-values 執行當下那一刻的值。某個時窗內 `/metrics` 收集不了時（例如兩個鍵撞成同一條 series），送達檢查只印一行 NOTICE，`generate-routes --output-configmap --strict` 因此在時窗內放行、寫出 ConfigMap，時窗外才拒寫；retire-drift 檢查以結束碼 2 失敗；describe-tenant 不顯示 `custom_alerts`。三者改讀新的 `da-guard served-values --config-facts`：每個租戶只輸出 exporter 不在任何時刻讀的 `custom_alerts`（含 `custom_alerts_error`、`custom_alerts_finding`）與 `metadata`，什麼都不 resolve、不 Gather，文件也不帶 `at`。新工具配不認得這個旗標的舊 da-guard 時，會要求升級或重建。
