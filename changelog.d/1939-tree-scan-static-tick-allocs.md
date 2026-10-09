---
section: Changed
topic: exporter
issues: [1939]
created: 2026-10-09T20:15:51+00:00
---
- **效能：threshold-exporter 安靜 tick 的配置量下降（exporter；[#1939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1939)）**：conf.d 掃描對 mtime fast-path 沿用的檔案直接共用上一次 scan 的紀錄，不再每檔重新配置；composite hash 改用可重用的 buffer。行為不變。⚠️ Go API：`pkg/config.TreeFile` 移除 `Reused`／`Parsed` 欄位，改由 `TreeScan.Reused(relKey)`／`TreeScan.Parsed(relKey)` 回答；`TreeFile` 在 walk 回傳後即不可變，且可能同時屬於多份 scan。
