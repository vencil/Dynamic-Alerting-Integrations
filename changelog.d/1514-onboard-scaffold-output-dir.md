---
section: Fixed
topic: cli-docs-accuracy
issues: [1514]
created: 2026-09-26T22:20:00+00:00
---
- **文件教的 `onboard`／`scaffold`／`blind-spot` 輸出旗標寫全，輸出說明改成工具實際寫出的東西（docs；[#1514](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1514)）**：`cli-reference` 的 `onboard` 一節依 parser 重寫（沒有位置參數、三個 phase 輸入、輸出目錄結構與結束碼）；遷移劇本改用 `--rule-files '<glob>'`／`--output-dir <目錄>`，預期輸出改成 `phase2-rules/migration-plan.csv` 等實際檔案；`tenant-lifecycle` 的 `scaffold` 改產到 `scaffold_output` 再只複製租戶檔，不再覆蓋 `conf.d/_defaults.yaml`，工具速查表的 scaffold／onboard／migrate／offboard／backtest 列改成工具真的收的參數；`migration-guide` 的 onboard 產出檔名改成實際的 `phase1-routing`／`phase2-rules`／`phase3-scrape`；`blind-spot --json` 寫全為 `--json-output`。
