---
section: Fixed
topic: confd-family
issues: [3965]
created: 2026-11-17T21:30:00+00:00
---
- **已宣告的 conf.d 樹判定後變動，一律點名拒絕，不再用較少的輸入作答（da-tools；[#3965](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3965)）**：判定改記 da-guard 那次 walk 的身分（`config_hash`），不再於判定後另外 stat 各檔；之後 da-guard 每份走過這棵樹的文件（served-values、effective、key-verdict、預設檢查、parse-tree 重讀）都比對，讀到另一棵樹就以結束碼 `2` 點名旗標，不論 da-guard 的結束碼。工具寫回 conf.d 時以判定的身分為前置條件（`da-guard write` 新增輸入欄位 `expect_config_hash`，另一棵樹回 `tree_changed`、什麼都不寫；輸出新增寫後的 `config_hash`）。da-guard 一次執行內各次 walk 讀到不同的樹時回 `2`。⚠️ 先前判定進行中或判定後少一個檔時，多數 conf.d 輸入照常以較少的輸入作答（量測見 #3965）。
