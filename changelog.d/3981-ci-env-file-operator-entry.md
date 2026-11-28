---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T18:03:07+00:00
---
- **寫 `$GITHUB_OUTPUT`／`$GITHUB_STEP_SUMMARY` 的工具改經 `write_operator_output`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`bench_verdict --github-output`、`check_release_immutable`（及共用其寫入的 `release_publish`）、`plan_race_shards`（`plan`／`merge`）、`file_race_report` 仍以附加方式寫入。目標寫不了（目錄不存在、是目錄、權限不足）時，原本印 traceback 加 `unexpected internal error`，現在是結束碼 `2` 加一行；`bench_verdict` 點名 `--github-output`，其餘的路徑來自環境變數，不點名旗標。`release_publish` 的寫入在發布之後，寫不了時發布已完成。目標是沒有讀端的 FIFO 時，原本卡住，現在是結束碼 `2`。寫入的檔案權限改為 `0600`（新檔，以及目前使用者擁有的既有檔；輸出路徑本身是符號連結時，其目標的權限不變，他人擁有的檔亦不變），原本新檔是 `0644`、既有檔不變。
