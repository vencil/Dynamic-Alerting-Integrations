---
section: Fixed
topic: da-tools
issues: [1200]
created: 2026-11-24T19:00:00+00:00
---
- **`diagnose` 逐叢集判定 exporter 健康（da-tools；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：健康查詢改為 `min by(tenant, da_cluster)`（[ADR-006](docs/adr/006-tenant-mapping-topologies.md)）。先前租戶在兩個叢集、其中一個完全沒有 `mysql_up` 時仍回 `healthy`。⚠️ **行為變更**：序列帶 `da_cluster` 時，DOWN 的訊息點名叢集（`Exporter reports DOWN in <叢集> (mysql_up!=1)`），沒宣告叢集也一樣；以 `mariadb@<叢集>` 宣告的叢集沒有 exporter 在回報時列 `Exporter absent in <叢集>`、結束碼 `1`（先前 `0`），判定同 `TenantExporterAbsent`：不帶 `da_cluster` 的序列對每個宣告的叢集都算存在。有逐叢集宣告或多個叢集的序列時輸出多 `exporter_clusters`。Pod 檢查只看 `kubectl` 目前的 context：租戶確實跨叢集（宣告兩個以上的叢集，或序列來自兩個以上的叢集）時，在那裡找不到 Pod 不列為問題，改記在 `pod_check`，`batch-diagnose` 文字報告多一行註記；其他情況仍是 `Pod not found`。`batch-diagnose` 對點名叢集的問題，建議改用該叢集的 context。序列不帶 `da_cluster` 且沒宣告叢集時，輸出與結束碼不變。只涵蓋 `mariadb`。
