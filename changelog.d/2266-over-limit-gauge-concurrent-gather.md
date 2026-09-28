---
section: Fixed
topic: exporter
issues: [652, 2266]
created: 2026-09-28T05:55:04+00:00
---
- **`da_tenant_metrics_over_limit` 不再在某些 scrape 整族消失或只剩部分 tenant（threshold-exporter；[#2266](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2266)）**：這個 gauge 原本是註冊在 registry 上、每次 scrape 由 collector 先 `Reset()` 再逐一 `Set()` 的 GaugeVec，但 `Registry.Gather` 會把各 collector 放在不同 goroutine 並行收集，GaugeVec 自己的輸出可能落在 Reset 與 Set 之間。現在改由 collector 在同一次收集裡直接輸出 const metric，metric 名稱、help 與 `tenant` label 都不變；依賴它的 `TenantMetricsOverLimit` 與 `DefaultsTruncationStorm` 不會再因此忽紅忽綠。
