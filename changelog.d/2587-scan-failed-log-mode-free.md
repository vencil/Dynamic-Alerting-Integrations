---
section: Fixed
topic: exporter
issues: [2587]
created: 2026-10-01T13:30:53+00:00
---
- **debounced reload 掃描失敗的 log 不再自稱 hierarchical（exporter；[#2587](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2587)）**：debounced reload 自己的掃描失敗（例如同一租戶 id 在兩個檔宣告）時印的 `ERROR: hierarchical scan failed: …` 改為 `ERROR: scan failed: …`。這次掃描在決定走扁平或階層路徑之前執行，沒有 `_defaults.yaml` 的扁平模式也會印這行，舊字樣會讓人誤以為樹裡有 `_defaults.yaml`。只改字串，`da_config_scan_failures_total` 與 reload 行為不變；用舊字串 grep log 或做 log-based 告警的請改用新字串（`scan failed` 兩版都配得到）。
