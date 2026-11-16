---
section: Changed
topic: rule-packs
issues: [3833]
created: 2026-11-16T17:30:12+00:00
---
- **⚠️ Oracle pack 改用 query-exporter，指標由 repo 出貨的查詢設定產生（rule-packs；[#3833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3833)）**：原本宣告的 iamseth/oracledb_exporter 已停止維護，而且 pack 讀的指標裡有四個是它與 Oracle 官方 exporter 都不輸出的，所以預設出貨的 `OracleHighSessionUtilization` 一直讀不到資料。現在請以 [albertodonato/query-exporter](https://github.com/albertodonato/query-exporter)（版本見 `requirements/ci-constraints.txt`）執行 `rule-packs/exporters/oracle.yaml`，再加上你自己的 databases 設定（資料庫名稱須為 `oracle`，一個 exporter 只接一個資料庫），監控帳號需要 `SELECT_CATALOG_ROLE`。改了兩條告警：`OracleDatabaseDown` 改判 liveness 查詢超過 60 秒沒成功，或從沒成功過；session 使用率改用全部 session 除以 `SESSIONS` 參數，因此在 PDB 裡也讀得到。新閘門 `check_exporter_queries.py` 用 query-exporter 自己的程式推導輸出名稱，pack 讀到推不出的名稱就會被擋；CI 另外對 Oracle Free 實跑這份設定。
