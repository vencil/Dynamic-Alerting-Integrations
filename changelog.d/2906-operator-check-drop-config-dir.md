---
section: Removed
topic: confd-family
issues: [2906, 3137]
created: 2026-10-20T13:58:19+00:00
---
- **⚠️ Breaking：`operator-check` 移除 `--config-dir` 旗標（tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：這個旗標宣告了但沒有任何檢查讀它（`cli-reference` 原本就註明「接受，但不影響結果」），工具也從不讀租戶 conf.d。現在帶 `--config-dir` 會被 argparse 拒絕、結束碼 2；把它從指令或 CI 腳本裡刪掉即可，五項檢查與輸出不變。縮寫一併失效：`--co X`、`--config X` 同樣結束碼 2；帶 `--json` 時這個拒絕來自 argparse，stdout 不會有 JSON envelope。⚠️ 另一個連帶變化：`--c` 以前同時是 `--ci` 與 `--config-dir` 的前綴、被判為 ambiguous（結束碼 2），現在唯一對應 `--ci`，照 `--ci` 語意執行（發現性 fail 時結束碼 1；kubectl 不可用等呼叫端錯誤仍為 2）——腳本若依賴 `--c` 被拒，請改寫（[#3137](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3137)）。
