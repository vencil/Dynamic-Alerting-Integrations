---
section: Changed
topic: confd-family
issues: [3206]
created: 2026-10-23T05:00:00+00:00
---
- **⚠️ Breaking：寫進已存在的輸出目錄前，先對那棵樹走共用的 conf.d 拒絕（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，ADR-036 §7 步驟 9）**：`scaffold`、`onboard`、`migrate`（`-o`，含 `--triage`、只給 `--scrape-config`）與 `da-assembler`（`--config-dir`），目錄已存在且有 da-guard 驗證會點名的路徑時，以前照寫、結束碼 0，現在結束碼 2、逐條點名、不寫。輸出目錄本身被當成整棵樹判：`-o conf.d/sub` 不看 `sub` 旁邊的路徑，要判整個 conf.d 請傳根目錄。controller 每個 reconcile 週期判一次，被拒時 ERROR 點名、這個週期不寫不刪、行程繼續（`--once` 結束碼 2）；可服務的週期會刪掉標頭指名已不存在之 CR 的檔，watch 改從初始 list 的 resourceVersion 開始。`batch-diagnose`、`patch-config` 的 ConfigMap 鍵改由 `da-guard parse-tree --names` 分類，沒有 da-guard 時結束碼 2。
