---
section: Fixed
topic: da-tools
issues: [1200]
created: 2026-11-24T19:00:00+00:00
---
- **`diagnose` 逐叢集判定 exporter 健康（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：健康查詢改為 `min by(tenant, da_cluster)`（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。先前租戶在兩個叢集、其中一個完全沒有 `mysql_up` 時仍回 `healthy`，一個叢集是 0 時只說 DOWN、不說是哪個。現在 DOWN 會點名叢集（`Exporter reports DOWN in <叢集> (mysql_up!=1)`）。⚠️ **行為變更**：租戶以 `mariadb@<叢集>` 逐叢集宣告時，宣告的叢集沒有 exporter 在回報就列 `Exporter absent in <叢集>`、結束碼 `1`（先前 `0`）；判定與 `TenantExporterAbsent` 告警相同，不帶 `da_cluster` 的序列對每個宣告的叢集都算存在。有逐叢集宣告或序列來自多個叢集時，輸出多 `exporter_clusters`（各叢集的狀態）與 `pod_check`（Pod 檢查只看 `kubectl` 目前的 context）；這時在目前的 context 找不到 Pod 不再列為問題。單一 Prometheus、沒宣告叢集的輸出與結束碼不變。只涵蓋 `mariadb`。
