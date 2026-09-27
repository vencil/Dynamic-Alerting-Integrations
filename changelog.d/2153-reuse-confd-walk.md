---
section: Changed
topic: tenant-api
issues: [2153]
created: 2026-09-27T16:48:19+00:00
---
- **寫入前的 conf.d 全樹掃描改為沿用上一次的掃描結果（tenant-api；[#2153](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2153)）**：檢查「tenant id 是否已由其他檔宣告」（#2078）時，每次都會把 conf.d 裡每個檔重新讀取、完整解析一遍，檔案多或單檔大時，每筆寫入都要付一次冷掃描的成本。現在改用 exporter reload 既有的 mtime 快速路徑：tenant-api 保留上一次完整完成的掃描結果，沒變的檔不再讀取與解析，判定規則與 exporter 相同。逾時或失敗的掃描不會被沿用。PR 模式的 batch 寫入也從「每個 op 掃一次全樹」改為整個 batch 只掃一次。第一次掃描（冷掃描）的成本不變。
