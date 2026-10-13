---
section: Changed
topic: da-tools
issues: [1516]
created: 2026-10-10T17:30:00+08:00
---
- **⚠️ Breaking：`config-diff` 改為比較兩側的渲染結果（ADR-037；[#1516](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1516)、[#1405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1405)、[#2120](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2120)）**：base 與 PR 兩側各跑一次 `da-guard served-values` 與路由產生器，比較 exporter 送出的值與完整的 Alertmanager 設定（route 節點、receiver、inhibit rules），所以平台預設、profile、根目錄平台檔的變更會報出受影響的租戶。`_` 開頭檔改以 parse-tree 讀出的值逐鍵比較（只改註解不算變更），值變了的鍵列在 Platform Settings 與 JSON 的 `platform_settings`，`uncovered_files` 改為有待審平台設定的檔。新增 `--at`（讀排程、靜默與維護時段的時間點，兩側共用；沒給時取執行當下），`da-tools init` 產生的 CI 傳 base commit 的時間。Markdown 報告每個區段同一個變更只列一次，附受影響的租戶數與前 5 個名字（同一個平台變更改到 1000 個租戶也只占一行），不再逐租戶列。JSON 改為 `schema: da-tools.config-diff/v1`，每筆變更帶它影響的租戶，舊的 `metric_diffs`、`profile_diffs` 等已移除，解析舊 JSON 的腳本要跟著改。一律需要 da-guard（映像內建）。PR 側有檔讀不了時結束碼 2 並點名該檔，不再讀成「閾值被移除」；base 側設定已壞時該面向列為未比對（結束碼 1），不擋修好它的 PR。重新產生的 CI 需要搭配本版以後的映像。
