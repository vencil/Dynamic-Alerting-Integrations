---
section: Fixed
topic: docs
issues: [1200]
created: 2026-11-28T09:54:15+00:00
---
- **receiver 沒寫 `send_resolved` 時的行為，文件與 schema 改成實際情況（docs；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200)）**：BYO Alertmanager 整合指南原本寫「預設 false」，`tenant-config.schema.json` 則六種類型都寫 `Default: true`。路由產生器不替這個欄位補預設值，沒寫時用的是 Alertmanager 對該類型的預設：`webhook`、`rocketchat`、`teams`、`pagerduty` 是 `true`（一次觸發到恢復送兩則通知），`email`、`slack` 是 `false`。指南改列各類型對照表，schema 中 `email` 與 `slack` 的說明與 `default` 註記改為 `false`。行為沒有改變；恢復通知會影響下游時請明寫這個欄位。另補一句：`send_resolved:` 留空會輸出成 null，Alertmanager 讀成 `false`，不等於沒寫。
