---
section: Fixed
topic: platform
issues: [1291]
created: 2026-10-31T16:49:47+00:00
---
- **Prometheus 的 egress NetworkPolicy 放行 Vector 的 9598 與 9599（[#1291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1291)）**：`allow-prometheus-egress` 原本沒列這兩個埠，Prometheus 抓不到 Vector 的自身指標與 projection-gate 判定，四條 Vector 平台告警結構上永遠不會觸發。現在任何 namespace 的 Vector 都放行。新增契約測試：平台 chart 標給 Prometheus 抓的每個埠都必須在 egress 放行清單裡，漏一個就紅。`VectorBufferEventsDropped` 補上 promtool 觸發測試。Vector chart 的 `metrics.enabled` 預設仍是 false，前兩條告警要開了它才有資料。
