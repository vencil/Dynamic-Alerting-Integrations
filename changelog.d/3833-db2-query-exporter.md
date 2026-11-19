---
section: Changed
topic: rule-packs
issues: [3833]
created: 2026-11-19T09:00:00+00:00
---
- **⚠️ DB2 pack 改用 query-exporter，指標由 repo 出貨的查詢設定產生（rule-packs；[#3833](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3833)）**：原本宣告的 exporter 不輸出 pack 讀的 `db2_*` 名稱。請以 query-exporter（版本見 `requirements/ci-constraints.txt`；官方 image 已含 DB2 driver，僅 x86_64）執行 `rule-packs/exporters/db2.yaml`，databases 設定要有 `db2` 與 `db2_liveness` 兩個同 DSN 的項目（liveness 查詢獨用一條連線），一個 exporter 一個資料庫（pureScale／DPF 每個 member 一個），監控帳號要對 `MON_GET_DATABASE`、`MON_GET_TRANSACTION_LOG`、`MON_GET_TABLESPACE` 明確授予 `EXECUTE`（`SYSMON` 不含）。自建 exporter 的原始指標名改變：`db2_connections_active` → `db2_connections_current`；`db2_up` 改由 liveness 查詢的 `query_timestamp` 判斷；命中率與 log 使用率由 pack 自己算（IBM 的 pages found ÷ logical reads；used ÷（used + available），無限日誌時不計）；sort 改讀 `db2_sorts_total`、`db2_sort_overflows_total`。tablespace 不含 SMS、temp 與沒有上限的 auto-resize，預設資料庫因此不出列（[#4093](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4093)）。`DB2DatabaseDown` 偵測約 90–105 秒；新增 `DB2QueryStalled`。
