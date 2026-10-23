---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T05:00:00+00:00
---
- **⚠️ Breaking：寫 conf.d 檔的工具指向已存在的目錄時，先對那棵樹走共用的 conf.d 拒絕（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`scaffold`、`onboard`、`migrate` 的 `-o`／`--output-dir` 是已存在的目錄、且裡面有 da-guard 驗證會點名的路徑時，以前照寫、結束碼 0，現在結束碼 2、逐條點名、一個檔都不寫。目錄不存在、或是空目錄／只有不是設定檔的檔案（da-guard 判為可服務）時照常寫入。`batch-diagnose` 找租戶與 `patch-config` 判斷 ConfigMap 鍵（租戶鍵、`_defaults` 鍵、exporter 讀哪些鍵）改用 `da-guard parse-tree --list-only --names` 的分類，執行環境要有 da-guard（da-tools 映像已內附）；沒有時 `batch-diagnose` 結束碼 2，`patch-config` 在寫入之前結束碼 2。
