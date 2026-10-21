---
section: Changed
topic: exporter
issues: [3180]
created: 2026-10-20T16:36:17+00:00
---
- **`da-guard` 的兩支窮舉矩陣測試改為同一組平台檔共用一個暫存目錄（exporter；[#3180](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3180)）**：`TestGuard_RedundantAdviceNeverMovesMetricsAcrossAliasSpellings` 與 `TestGuard_NullOverrideIsNoWriteOnEveryLayer` 原本每一格都開新的暫存目錄、重寫整棵設定樹；現在平台檔、profile 檔只在它們變化的那一層寫一次，每格只覆寫租戶檔；與內層值無關的基準觀測也只算一次。格子、斷言與 `-short` 版本都沒變，PR CI 跑 threshold-exporter Go 測試的時間因此縮短。等價格合併這類需要懂語意的加速另行處理。
