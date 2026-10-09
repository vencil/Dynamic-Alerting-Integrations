---
section: Changed
topic: exporter
issues: [1939]
created: 2026-10-09T20:15:51+00:00
---
- **效能：threshold-exporter 每次 conf.d 掃描少一輪逐檔配置（exporter；[#1939](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1939)）**：計算 composite hash 時改用一個可重用的 buffer，不再為每個檔案的 hash 各複製一份。1000 檔的樹每次 tick 約少 1000 個配置。行為與 hash 值不變。
