---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T05:00:00+00:00
---
- **⚠️ Breaking：寫進已存在的輸出目錄前，先對那棵樹走共用的 conf.d 拒絕（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`scaffold`、`onboard`、`migrate`（含 `--triage`、只給 `--scrape-config` 等不寫 conf.d 檔的模式）的 `-o`，以及 `da-assembler --render-cr` 的 `--config-dir`，目錄已存在且有 da-guard 驗證會點名的路徑時：以前照寫、結束碼 0，現在結束碼 2、逐條點名、不寫任何檔。controller 模式每個 reconcile 週期（一次 `--once`、一個 watch 事件）重新判一次，被拒時 ERROR 點名、CR 狀態 Error、這個週期不寫也不刪，行程不結束。目錄不存在、空目錄或只有非設定檔時照常寫入。`batch-diagnose` 與 `patch-config` 的 ConfigMap 鍵分類改用 `da-guard parse-tree --list-only --names`，沒有 da-guard 時以 2 結束（`patch-config` 在寫入前）。
