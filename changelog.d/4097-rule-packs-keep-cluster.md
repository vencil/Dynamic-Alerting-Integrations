---
section: Changed
topic: rule-packs
issues: [4097]
created: 2026-11-21T12:00:00+00:00
---
- **rule pack 的規則逐叢集計算（[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)，ADR-006）**：同一個租戶跑在兩個叢集時，原本的規則把兩邊的序列合在一起算——健康的叢集會遮住壞掉的那個（「全部 down」取 max、「沒有 primary」被另一邊的 primary 抵銷、平均延遲被稀釋）。現在聚合與配對都保留 `da_cluster`，告警每個（租戶，叢集）一條並帶 `da_cluster` label；閾值、維護狀態仍是租戶一份。**只評估自己一個叢集的 Prometheus 行為不變**（序列上沒有 `da_cluster`，分組等同逐租戶）。在中央評估多個叢集的資料時要注意兩件事：(1) 加總型的閾值（請求速率、slow query、deadlock、GC pause 等 `sum` 類）改成對單一叢集的量比較，原本兩個叢集加起來才超過的不再觸發；(2) `MariaDBNoPrimary` 對跨叢集的複寫組（primary 在一個叢集、replica 在另一個）不誤報，前提是 replica 的 exporter 有送 `mysql_slave_status_*`（預設開啟）。`TenantExporterUndeclared` 會指出是哪個叢集的 exporter。尚未逐叢集：`TenantExporterReplicaDown`、`TenantExporterAbsent`、`MassExporterOutage`、兩條設定失效哨兵（等逐叢集存在判定）、custom recipe 編譯出的規則。通知分組（`group_by`）預設不按叢集拆（這是決定，見 alertmanager-routing 條目）；warning／critical 去重在兩則告警都帶 `da_cluster` 時逐叢集，都不帶時仍只比租戶與 `metric_group`。
