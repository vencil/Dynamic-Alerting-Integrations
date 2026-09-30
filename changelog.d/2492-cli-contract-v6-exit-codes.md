---
section: Fixed
topic: docs
issues: [2492]
created: 2026-09-30T00:27:05+00:00
---
- **cli-reference 結束碼表：列出的碼程式要回得出來（[#2492](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2492)）**：`check_cli_contract` 新增 V6，結束碼表列了某個非零碼、程式卻沒有任何出口回得出來時會擋下；只有 traceback 會給的 1 要在該列註明並豁免。同時更正 batch-diagnose（租戶檢查失敗目前也回 0，[#2493](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2493) 追蹤）、migrate（輸入無效是 2 不是 1）、onboard、config-diff、evaluate-policy 的觸發條件。
