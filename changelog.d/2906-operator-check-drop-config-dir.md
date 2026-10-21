---
section: Removed
topic: confd-family
issues: [2906]
created: 2026-10-20T13:58:19+00:00
---
- **⚠️ Breaking：`operator-check` 移除 `--config-dir` 旗標（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：這個旗標宣告了但沒有任何檢查讀它（`cli-reference` 原本就註明「接受，但不影響結果」），工具也從不讀租戶 conf.d。現在帶 `--config-dir` 會被 argparse 拒絕、結束碼 2；把它從指令或 CI 腳本裡刪掉即可，五項檢查與輸出不變。
