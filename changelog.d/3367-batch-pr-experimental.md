---
section: Changed
topic: migration-toolkit
issues: [3367, 4211]
created: 2026-11-22T07:54:50+00:00
---
- **⚠️ `da-tools batch-pr apply` 標為 experimental，文件改寫成現況（docs；[#3367](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3367)）**：`apply` 必填的 `--plan`（Plan JSON）與 `--emit-dir` 沒有任何出貨的工具產生。Profile Builder（`BuildProposals`／`EmitProposals`）只是 library，沒有接到任何 CLI；它的輸出採「一個 cluster 一個目錄」，同一租戶出現在兩個 cluster 時會被 da-guard 以重複租戶拒收。先前文件寫「由 batch-pr 管線消費」「全自動導入」都不成立；portal 匯入流程第 4 步的範例指令用了不存在的 `--profile`／`--targets`／`--chunk-by`，照抄會 rc 2。導入客戶規則請用逐租戶的 `da-tools migrate`。大量導入在 [#4211](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4211) 重新選型，`batch-pr` 與 Profile Builder 可能整組退役。
