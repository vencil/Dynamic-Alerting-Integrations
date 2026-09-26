---
section: Added
topic: custom-alerts
issues: [1092]
created: 2026-09-26T17:00:00+00:00
---
- **Custom Alerts 新增 `slo_burn_rate` recipe、`==` 運算子與按租戶投遞（custom alerts）**：第 7 個平台 recipe `slo_burn_rate` 讓租戶只宣告 `metric`／`denominator_metric`／`objective`（零 PromQL），編譯期展開為多窗 SLI recording 與 fast（critical）／slow（warning）multi-window burn-rate 告警，含低流量 `min_events` 下限；v1 僅支援 availability／error-ratio 型（[#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092)）。`threshold` recipe 的 `op` 新增 `==`，用於以指標值表達的狀態碼／錯誤碼，採 any-match 語意（任一副本等於該代碼即觸發），並補上撰寫與 exporter 存活性的採用指引。custom 告警改為依租戶投遞到既有的 `tenant-<name>` receiver；沒有有效 `_routing` 的租戶維持 `custom-alerts-firehose`（不通知）。
