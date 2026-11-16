---
section: Added
topic: tools
issues: [3830, 3832]
created: 2026-11-14T11:56:42+00:00
---
- **validate-config 說明平台預設啟用的閾值在你的樹裡怎麼處理（`platform_defaults`；[#3830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3830)、[#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)）**：讀取端內建了平台預設啟用的閾值（見 #3832 的條目），新的一列逐鍵列出狀態：`builtin`（根 `_defaults.yaml` 沒給值，由內建層供給，附平台值與拿到它的租戶數）、`overridden`（根層給了數字，附那個數與平台值；兩者相同時註明），第一行印出內建表摘要，用來比對 exporter、tenant-api 與 da-tools 帶的是不是同一張表。答案全部來自 da-guard（`da-guard effective` 新增頂層欄位 `builtin_defaults`），不另讀 threshold registry。樹裡沒寫某個預設啟用鍵是正常狀態，所以這一列是 PASS；根層讀不了時是 WARN，說明內建層已撤掉、沒有任何租戶拿到預設列；判不了時是 WARN「not checked」。不影響 exit code。
