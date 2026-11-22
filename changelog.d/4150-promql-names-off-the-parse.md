---
section: Fixed
topic: tools
issues: [4150]
created: 2026-11-22T09:37:36+00:00
---
- **孤兒 recording rule 檢查、alert reference、指標字典檢查改從 Prometheus 的語法樹取指標名（tools；[#4150](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4150)）**：三支工具原本以文字掃描 PromQL，再用手抄的函式與關鍵字清單排除非指標（清單還列著 Prometheus 3 已移除的 `holt_winters`）；指標字典檢查的 regex 只認小寫，漏掉 ClickHouse 的大小寫混合指標，多報 4 條過時條目。現在一律由 da-read 讀取（`_lib_consumer_read.promql_metric_names`），清單全數刪除。dashboard 查詢先依 Grafana 的變數語法代入再解析（只讀 query 型的 template 變數，datasource 照 Grafana 的繼承規則）；非 Prometheus datasource 的查詢略過並在報告列出；仍解析不了的查詢結束碼 `2`，點名 dashboard。用到未啟用的實驗性函式的查詢同樣會被拒。
