---
section: Added
topic: cli-docs-accuracy
issues: [1379, 1495]
created: 2026-09-26T17:00:00+00:00
---
- **文件指令對著活的 CLI 契約機械驗證（lint）**：新增 `check_cli_contract`，直接讀每個子命令真正的 argparse parser，比對文件 code block、inline 指令、manifest 與 portal CLI Playground 裡的命令，攔下未知子命令、未宣告或縮寫旗標、幻影選項列與未列的結束碼；code block 裡的命令另外整條交給真的 parser，攔下缺必填、多出來的位置參數、`choices`／型別不收的值與互斥旗標（[#1379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1379)）；`check_cli_default_drift` 比對 cli-reference 的預設值欄；`check_doc_datools_cmds` 要求可寫掛載的 `docker run` 必須帶 `--user` 且排在 image 之前。既有文件債以帳本列管、只縮不脹，讓文件教的指令不再與工具漂移。
