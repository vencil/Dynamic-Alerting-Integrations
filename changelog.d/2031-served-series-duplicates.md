---
section: Fixed
topic: exporter
issues: [2031]
created: 2026-10-09T05:29:45+00:00
---
- **重複 series 不再讓整個 `/metrics` 回 500（threshold-exporter / da-guard；[#2031](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2031)）**：`da_config_event` 新增 `metric_key` label（`threshold_expired` 填該閾值的 key，其他事件為空字串；`reason` 文字不變），兩個過期 key 的 reason 字串相同時不再撞成同一列。`_custom_alerts` 裡送出同一個 series 的第二項（同名同形，或同形的兩個 `slo_burn_rate`）改為單項丟棄並計入 `da_custom_alert_parse_errors`，其他租戶照常送出。⚠️ 行為變更：da-guard 主 gate 新增兩個 error——`custom_alert_duplicate_series`（與 exporter 丟棄的是同一批），以及 `metrics_not_gatherable`（exporter 的 `/metrics` 對整棵樹無法 Gather，例如 `X: "70:critical"` 與 `X_critical` 並存、`q=~` 與 `q_re=` 並存；不受 `--scope` 限制、涵蓋一天中每個排程時段）——含這些形狀的既有樹由 exit 0 改為 exit 1。`served-values` 結束碼 2 的訊息點名了 key 時，不再附上 client_golang 的原始錯誤（其中的值與順序每次執行不同）。
