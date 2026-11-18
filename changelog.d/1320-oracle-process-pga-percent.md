---
section: Changed
topic: rule-packs
issues: [1320]
created: 2026-11-17T16:30:00+00:00
---
- **⚠️ breaking：Oracle 的 process 與 PGA 閾值改為佔上限的 %，key 改名且不設 alias（[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：`oracle_process_count`（絕對 process 數）與 `oracle_pga_allocated_bytes`（絕對位元組）退役，改為以下兩個 key：
  - `oracle_process_usage_percent`：佔 `PROCESSES` 參數的 %，**預設啟用、值 85**，所以 `OracleHighProcessCount` 現在對每個 Oracle 租戶都會響。
  - `oracle_pga_usage_percent`：佔 `PGA_AGGREGATE_LIMIT` 的 %，仍需自行設定，建議起點 85；`PGA_AGGREGATE_LIMIT` 為 0（未設上限）的資料庫不計算比例。

  比例按 exporter 分別計算，再取最滿的那台。單位變了、舊值不能照搬，寫了舊 key 會被判 `retired` 並點名新 key。alertname 不變。recording rule 改為 `tenant:oracle_process_usage:percent`、`tenant:oracle_pga_usage:percent`。`rule-packs/exporters/oracle.yaml` 新增 `oracledb_processes_limit`、`oracledb_pga_aggregate_limit_bytes`，要重新部署這份查詢設定。接在 PDB 時看不到其他容器的用量，比例是下限（PDB 自設 `PGA_AGGREGATE_LIMIT` 時 PGA 比例才準），要看整個上限請接 CDB root。上限為 0 時 PGA 閾值不會響、目前沒有提醒（[#4044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4044)）。本版內建表摘要 `522437716e2a`。
