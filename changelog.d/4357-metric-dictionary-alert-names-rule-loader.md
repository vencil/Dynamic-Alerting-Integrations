---
section: Fixed
topic: lint
issues: [4357]
created: 2026-11-28T16:06:56+00:00
---
- **`check_metric_dictionary` 的告警名改由 Prometheus 的 rule loader 讀出（lint；[#4357](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4357)）**：檢查 `maps_to`／`golden_rule` 契約時用的告警名，原本以 regex 掃 rule pack 的 YAML 原文，寫成 `- {alert: X}` 的告警讀不到、帶引號的名稱連引號一起讀，block scalar 裡的 `- alert:` 文字被當成告警。現在改用 rule loader 讀出的 `alert`；loader 拒收的 pack 一律 rc 2 並點名該檔。
