---
section: Changed
topic: alertmanager-routing
issues: [2431]
created: 2026-09-30T12:22:47+00:00
---
- **`routes[].match` 的值與 override 的 `alertname`／`metric_group` 未加引號、讀成非字串時，`--strict`、da-guard、tenant-api 報錯（[#2431](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2431)）**：`yes`／`on`（布林）、`1:30`（整數 90）、`2001-12-15`（日期）、`~`（null）、`!!int 5`、`alertname: 123` 這類值，generator（PyYAML）與 Go 端（yaml.v3）原本讀法不同；Go 端現在照 PyYAML 讀這些值。`generate-routes --strict`（含 `--validate --strict`）與 `validate-config --strict` 每個值一行 `ERROR`、結束碼 1，da-guard 報 `routing_value_not_string`（error），tenant-api 的 PUT 對 body 自己寫的值回 400 `INVALID_BODY`（從 `_routing_defaults`／routing profile 繼承的值與 batch 不判）。match 的 key 不是合法 label 名稱時整條照舊略過，值不另判。不加 `--strict` 時 generator 行為不變。修法：加引號，例如 `team: "yes"`、`alertname: "123"`。
