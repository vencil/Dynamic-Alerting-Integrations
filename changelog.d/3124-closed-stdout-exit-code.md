---
section: Changed
topic: da-tools
issues: [3124]
created: 2026-10-17T07:49:03+00:00
---
- **⚠️ 結束碼變更：讀端先關掉 pipe 時由 `120` 改為 `2`（[#3124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3124)）**：`da-tools <cmd> | head -c1`、`… | true` 的讀端先離開，或執行中 fd 1 被關，而工具還有輸出沒送出時，先前不論工具原本要以 0、1、2 或 crash 結束，行程都以直譯器自己的 `120` 結束；輸出量大時寫入失敗還會被報成 `ERROR: unexpected internal error (BrokenPipeError)`。現在一律以呼叫端錯誤 `2` 結束（`tenant-verify` 是 `1`），stderr 一行 `ERROR: stdout was closed before all output was written`，不是 crash、沒有 traceback。輸出在讀端關閉前已全部送出時，結束碼照舊；磁碟滿、`/dev/full` 這類寫入失敗印 `ERROR: cannot write to stdout: <原因>`，同樣是 `2`。shell 在啟動前就關掉 stdout（`>&-`）不受影響：輸出被丟棄、結束碼照舊。不是 stdout 的 `BrokenPipeError`（例如 socket）仍是 crash。CI 若用 `120` 判斷「讀端提前離開」，請改看 `2` 與這一行。
