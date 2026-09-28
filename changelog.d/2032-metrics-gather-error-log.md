---
section: Fixed
topic: exporter
issues: [2032]
created: 2026-09-28T08:21:30+00:00
---
- **`/metrics` gather 失敗時會記 log 並計數（exporter；[#2032](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2032)）**：兩個 key 產生同一條 series 之類的 gather 失敗，過去 `/metrics` 整次回 500，但 exporter log 裡什麼都沒有，只能從 `up == 0` 看出壞了、看不出原因。現在每次失敗都在 exporter log 印一行 `error gathering metrics:`，點出撞在一起的 series；`/metrics` 並新增兩條 series `promhttp_metric_handler_errors_total{cause="gathering"|"encoding"}`。失敗時整次 scrape 仍回 500（不改成回部分內容，以免重複 series 的值在 scrape 之間無聲跳動），所以這個計數器只在恢復後第一次成功 scrape 才看得到，供事後回溯；即時告警仍是既有的 `ThresholdExporterDown`。`da-guard served-values` 的輸出不變。
