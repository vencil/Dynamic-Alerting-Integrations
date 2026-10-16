---
section: Changed
topic: alertmanager-routing
issues: [2431]
created: 2026-09-30T12:22:47+00:00
---
- **`routes[].match` 的值與 override 的 `alertname`／`metric_group` 未加引號、讀成非字串時，`--strict` 報錯（[#2431](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2431)）**：`yes`／`on`（布林）、`1:30`（整數 90）、`2001-12-15`（日期）、`~`（null）、`!!int 5`、`alertname: 123` 這類值：`generate-routes --strict`（含 `--validate --strict`）與 `validate-config --strict` 每個值一行 `ERROR`、結束碼 1（finding kind `routing_value_not_string`）；da-guard 不判這一項，樹上有路由內容時列為未判定證據（exit 4）。match 的 key 不是合法 label 名稱時整條照舊略過，值不另判。不加 `--strict` 時 generator 行為不變。修法：加引號，例如 `team: "yes"`、`alertname: "123"`。
