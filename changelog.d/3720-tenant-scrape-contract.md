---
section: Fixed
topic: rule-packs
issues: [3720, 3680]
created: 2026-11-10T14:44:04+00:00
---
- **⚠️ breaking：租戶 exporter 的 scrape 契約統一為 `job="tenant-exporters"`，照 BYO／Operator 文件設定的健康租戶不再被 `TenantExporterAbsent` critical page（[#3720](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3720)、[#3680](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3680)）**：租戶 rule pack 選 `up{job="tenant-exporters"}`，但 BYO 文件教的是 `tenant-db-exporters`、Operator 的 ServiceMonitor 以 Service 名當 job，兩者都讓每個宣告 `db_type` 的健康租戶持續告警。**遷移**：BYO 把 scrape job 改名為 `tenant-exporters` 並改用 `role: endpointslice`、`keepequal` 留 annotation 指定的 port、不覆寫 `instance`；Operator 在 ServiceMonitor 的 `relabelings` 加 `targetLabel: job`、`replacement: tenant-exporters`（之後 `TenantExporterJobAbsent`、`MassExporterOutage` 也會由 `operator-generate` 輸出）。lab 設定同步改為逐 pod scrape：過去兩個 target 寫進同一條 `up` series，健康的會把停機的遮住數分鐘。scrape 閘門與 `tests/k8s/test_tenant_scrape_contract.py` 以同一個判準檢查 lab 設定與三份文件的範例。
