---
section: Changed
topic: da-tools
issues: [3107]
created: 2026-11-17T19:30:00+00:00
---
- **四支 ops 工具的單檔輸出旗標改走 operator 輸出；`-` 與空值改為結束碼 2（da-tools；[#3107](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3107)）**：`analyze-gaps -o`、`backtest -o`／`--markdown-output`、`batch-diagnose -o` 與 `validate --convergence-output` 交給 kernel 開檔，權限 `0600`。⚠️ 行為變更：(1) 這四支的 stdout 已有報告，給 `-`、`/dev/stdout` 或本次 stdout 重導的檔時 `0` → `2`（要名為 `-` 的檔請給 `./-`）；(2) 空值（沒設的變數）`0` → `2`；(3) 非 root 寫別人的可寫檔 `2` → `0`，權限不動；(4) symlink 的目標不再被改成 `0600`；(5) 沒有 reader 的 FIFO 由卡住改為 `2`；(6) 寫入途中出錯留空檔；(7) JSON 檔與 `--json` 印的位元組相同（多一個結尾換行）；報告含 NaN／Inf 時不再寫出非法 JSON，改為一行錯誤與 crash code。
