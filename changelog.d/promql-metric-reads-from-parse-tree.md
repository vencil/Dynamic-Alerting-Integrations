---
section: Changed
topic: tools
issues: [4219]
created: 2026-11-24T22:10:00+08:00
---
- **規則讀哪些指標一律由 Prometheus 的語法樹判定（tools；[#4219](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4219)）**：scrape gate、`check_exporter_queries`、`da-tools operator-generate`、rule pack 拆分驗證與 `config_diff`／`patch_config --diff` 的影響告警清單，改經 `da-read promql-shape` 讀運算式，刪除以 regex 加關鍵字清單掃文字的讀法。舊讀法漏讀大寫開頭的名字（`ALERTS`、`ALERTS_FOR_STATE`、ClickHouse 的 `ClickHouseMetrics_*` 等）與緊接逗號的指標，也會把字串裡的字當成指標或函式呼叫。Prometheus 自己產生的序列（`up` 與兩個告警序列）改由一份定義共用。Prometheus 的 parser 拒收的運算式改為 rc 2 並點名，不再當成「什麼都沒讀」。
