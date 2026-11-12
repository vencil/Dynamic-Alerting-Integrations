---
section: Added
topic: threshold-registry
issues: [1200, 1175, 3773]
created: 2026-11-11T15:38:44+00:00
---
- **每個閾值鍵都要寫明 chart 出不出貨、值能否跨部署沿用、以及卡住的原因（threshold registry 的 `arming`／`portability`／`blocker`；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) D-A A1、[#3773](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3773)）**：取代 `chart_default`。`arming` 為 `default` 或 `operator`；`portability` 為 `portable`、`ratio_pending`（絕對值，待改比值，[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）或 `site_specific`，非 `portable` 必附中英理由；`blocker` 記錄沒有資料源、聚合錯誤、尚未量測、值有爭議，並附追蹤 issue。出貨的 key 不能有 blocker；不是 `portable` 的要寫 `default_exception` 並附追蹤 issue（目前只有 `mysql_connections`、`mysql_threads_running`，[#3778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3778)）。Helm `values.yaml` 的 `thresholdConfig.defaults` 下新增生成的註解段，列出規則包會讀、chart 卻不出貨的 key 與原因（helm install 後這些告警不會響，[#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175)）；各規則包檔頭同樣附原因。reachability 閘門的 `NOT_CHART_ARMED` 手寫清單移除，改由 registry 推導。
