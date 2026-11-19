---
section: Added
topic: rule-packs
issues: [1174]
created: 2026-11-19T01:30:00+00:00
---
- **Oracle 新增 `OracleHighHardParseTime`：hard parse 耗時佔 DB time 的比例過高時發 warning（[#1174](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1174)）**：比例取自 `v$sys_time_model`，等於 AWR Time Model 裡「hard parse elapsed time」那一列的「% of DB Time」。不採用 soft parse %，因為同一個負載會隨 client 的 statement cache 讀成 90% 或 4%。也不採用 hard parse ÷ execute，因為旁邊同時跑的 PL/SQL 批次會把它稀釋約 97 倍。比例逐個 exporter 計算後取最大值；DB time 每秒增加不到 1 秒（AAS < 1）時不計算，否則閒置庫的零星查詢會把比例撐到接近 100%。新 key `oracle_hard_parse_time_percent` 沒有平台預設值，需由租戶自行設定，建議起點 20。資料庫的統計收集期間比例約 12%，在建議起點 20 以下不會觸發；若把門檻調到 12 以下，請用排程式閾值（`window`）避開統計收集時段，不要用維護模式：它會靜音整個租戶的告警。`rule-packs/exporters/oracle.yaml` 新增查詢 `oracle_time_model`，需重新部署這份查詢設定。
