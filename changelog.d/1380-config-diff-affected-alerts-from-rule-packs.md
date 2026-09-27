---
section: Fixed
topic: da-tools
issues: [1380]
created: 2026-09-27T10:12:52+00:00
---
- **`config-diff` 的 Affected Alerts 欄與 `patch-config --diff` 改從 rule pack 反查真正的告警名（da-tools；[#1380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1380)）**：原本把 key 的拼法轉成 CamelCase 猜名字（`mysql_connections` → `*MysqlConnections*`），猜出來的名字在 rule pack 裡不存在，CI 貼到 PR 的 blast-radius 留言也帶著它。現在列的是真的讀這個 key 的告警（例如 `MariaDBHighConnections`、`MariaDBSystemBottleneck`），包含只透過 recording rule 間接讀的 `container_*`；沒有告警讀的 key 寫 `—`，找不到 rule pack 時寫 `unknown` 並在 stderr 說明，不再猜。da-tools image 因此一併出貨引用閾值的 rule pack。`patch-config --diff --json` 的 `affected_alerts` 在量不到時是 `null`，與「沒有告警讀它」的 `[]` 分開。
