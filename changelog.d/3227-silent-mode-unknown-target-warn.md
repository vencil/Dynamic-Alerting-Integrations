---
section: Fixed
topic: exporter
issues: [3227]
created: 2026-10-22T00:09:32+00:00
---
- **結構化 `_silent_mode` 的未知 target 現在會寫 WARN（exporter；[#3227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3227)）**：`_silent_mode: {target: purple}` 這類 exporter 不認得的 target 先前被靜默略過——什麼都沒靜音、log 也沒有任何一行。現在與純量形式的未知值一樣，寫一條 `WARN: unknown silent mode target ... ignoring (valid: warning, critical, all, disable)`。靜音行為不變：未知 target 仍然不靜音任何東西。
