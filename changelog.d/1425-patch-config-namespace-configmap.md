---
section: Changed
topic: da-tools
issues: [1425]
created: 2026-10-30T13:00:37+00:00
---
- **`da-tools patch-config` 新增 `--namespace`／`--configmap`（[#1425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1425)）**：threshold-exporter chart 改落在 release namespace、ConfigMap 也可由 `thresholdConfig.existingConfigMap` 指定後，patch-config 原本寫死的 `monitoring`／`threshold-config` 會改到錯的地方。兩個旗標的預設值與以前相同；`--exporter-namespace` 沒指定時改為跟著 `--namespace`（chart 把 ConfigMap 與 exporter 放在同一個 namespace）。
