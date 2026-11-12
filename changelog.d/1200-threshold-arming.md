---
section: Added
topic: threshold-registry
issues: [1200, 1175]
created: 2026-11-11T15:38:44+00:00
---
- **每個閾值鍵都要寫明 chart 為什麼出貨或不出貨它的值（threshold registry 的 `arming`；[#1200](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1200) D-A A1）**：取代原本的 `chart_default`，值為 `default`（chart 出貨即啟用）、`ratio_pending`（絕對值，待改比值，[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）、`site_specific`（沒有普世的值）、`pending_validation`（有候選值、尚未驗證）；非 `default` 必附中英各一行的理由，`<base>_critical` 不寫、跟隨 base。Helm `values.yaml` 的 `thresholdConfig.defaults` 下新增生成的註解段，列出 34 個規則包會讀、chart 卻不出貨的 key 與原因（helm install 後這些告警不會響，[#1175](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1175)）；各規則包檔頭同樣多一行「未啟用」原因。reachability 閘門的 `NOT_CHART_ARMED` 手寫清單移除，改由 registry 推導。
