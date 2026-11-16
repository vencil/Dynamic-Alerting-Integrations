---
section: Changed
topic: rule-packs
issues: [3833]
created: 2026-11-16T17:30:12+00:00
---
- **⚠️ Oracle pack 改用 query-exporter，指標由 repo 出貨的查詢設定產生（rule-packs；[#3833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3833)）**：原本宣告的 iamseth/oracledb_exporter 已停止維護，pack 讀的四個指標它與 Oracle 官方 exporter 都不輸出，預設出貨的 `OracleHighSessionUtilization` 因此從未觸發。請以 [query-exporter](https://github.com/albertodonato/query-exporter)（版本見 `requirements/ci-constraints.txt`）執行 `rule-packs/exporters/oracle.yaml`，配上自己的 databases 設定（資料庫名稱 `oracle`，一個 exporter 一個資料庫，保留內建指標），監控帳號需 `SELECT_CATALOG_ROLE`。`OracleDatabaseDown` 改判 liveness 查詢 90 秒沒成功或從未成功；session 使用率改為「所連 container 計入 `SESSIONS` 的 session ÷ 該參數」，PDB 內不再算進 CDB 的背景 session。新增 `OracleQueryStalled`：資料庫有回應但某支查詢被 exporter 停用時發 warning。閘門 `check_exporter_queries.py` 以 query-exporter 自己的程式推導名稱，CI 另對 Oracle Free 實跑比對。
