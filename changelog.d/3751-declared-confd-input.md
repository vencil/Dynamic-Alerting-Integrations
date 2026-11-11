---
section: Changed
topic: da-tools
issues: [3751, 3695]
created: 2026-11-11T19:30:00+00:00
---
- **⚠️ Breaking：conf.d 輸入旗標由宣告判定一次、拒收一律點名旗標（da-tools；[#3751](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3751)，[#3695](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3695) 第 1 批）**：explain-keys `--config-dir`、tenant-verify `--conf-d`、drift-detect `--dirs`、config-history `--config-dir` 改以 `_lib_input.add_input` 宣告，由同一處依序判定路徑、da-guard 能否列出根目錄、有沒有任何設定檔；後兩種先前轉印 da-guard 的原文（`no .yaml files found in <path>`、只點名路徑），現在點名旗標。drift-detect `--dirs` 的空項（`a,,b`）改為結束碼 2、項目前後的空白不再去掉（與 URL 清單旗標同一條規則），且判定後不再自己 `is_dir` 一次——判定之後才消失的目錄不會被讀成空的、把另一邊的每個檔都報成漂移。config-history 的 `log`／`show`／`diff` 不再在給的路徑旁建立 `.da-history/`；`.da-history/` 與 `--config-dir` 都不存在時結束碼 2、兩者一起點名（先前印「尚無快照記錄」、結束碼 0），conf.d 被刪掉之後仍讀得到它的快照歷史。
