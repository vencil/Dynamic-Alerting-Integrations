---
section: Fixed
topic: da-tools
issues: [3821]
created: 2026-11-21T12:37:55+00:00
---
- **da-tools 分派層寫不出 stderr 時仍以 rc 2 結束（[#3821](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3821)）**：未知命令、找不到工具腳本、工具崩潰的後備回報、stdout 寫入失敗這幾條分派層自己的 stderr 訊息，在 stderr 的讀端已關閉（接 stderr 的程序已先結束）時，原本結束碼會變成 120（直譯器最後一次 flush 失敗）或 1（`PYTHONUNBUFFERED=1` 下未攔截的 `BrokenPipeError`）。現在寫入改為盡力而為：失敗時丟掉訊息、把 stderr 導向 null device，結束碼維持 2。被分派出去的工具本身的行為不變。
