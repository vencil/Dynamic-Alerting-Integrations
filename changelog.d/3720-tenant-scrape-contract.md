---
section: Fixed
topic: rule-packs
issues: [3720, 3680]
created: 2026-11-10T14:44:04+00:00
---
- **⚠️ breaking：租戶 exporter 的 scrape 契約統一為 `job="tenant-exporters"`，照 BYO／Operator 文件設定的健康租戶不再被 `TenantExporterAbsent` critical page（[#3720](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3720)、[#3680](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3680)）**：租戶 rule pack 選 `up{job="tenant-exporters"}`，但 BYO 文件教的是 `tenant-db-exporters`、Operator 的 ServiceMonitor 以 Service 名當 job，兩者都讓每個宣告 `db_type` 的健康租戶持續告警。**遷移**：BYO 把 scrape job 改名為 `tenant-exporters` 並改用 `role: endpointslice`、`keepequal` 留 annotation 指定的 port、不覆寫 `instance`；Operator 在 ServiceMonitor 的 `relabelings` 加 `targetLabel: job`、`replacement: tenant-exporters`（之後 `TenantExporterJobAbsent`、`MassExporterOutage` 也會由 `operator-generate` 輸出）。逐 pod scrape 需 Prometheus ≥ 2.41 與 `endpointslices` 權限，`prometheus.io/port` 要寫 pod 的 port，`instance` 變成 `<pod IP>:<port>`（依 `instance=<namespace>` 的 silence／dashboard 改用 `tenant`）。
