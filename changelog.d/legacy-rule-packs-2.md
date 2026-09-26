---
section: Changed
topic: rule-packs
issues: [1176, 1196, 1231, 1320]
created: 2026-09-26T17:00:00+00:00
---
- **告警分級與閾值 key 校準（⚠️ 行為變更；rule packs、helm）**：MongoDB／MariaDB 存活告警改為 HA-aware 三層——單節點 `<DB>Down` 由 critical 降為 warning，新增 `<DB>ClusterDown` 與 `<DB>NoPrimary`（critical），正常 failover 不再誤 page。`MariaDBHighCPU` 正名為 `MariaDBHighThreadsRunning`（量的是並發執行緒飽和、不是主機 CPU%），平台預設由 80／120 校準為 30／50，Helm chart 預設同步改為 30；config key `mysql_cpu` 更名為 `mysql_threads_running`，舊拼寫在 2 個 release 的過渡窗內照收並雙發 series（[#1231](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1231)）。PostgreSQL 的 `pg_connections`／`pg_replication_lag` 成為 chart 出貨預設，另有 9 個原本沒接上的閾值 key 接通；已自訂這些值的部署不受影響。參考庫已測到反例的建議值，各出貨面會附上警語（[#1176](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1176)）。
