---
section: Changed
topic: da-tools
issues: [3124, 3156]
created: 2026-10-17T07:49:03+00:00
---
- **⚠️ 結束碼變更：寫 stdout 失敗時由 `120`／crash 改為 `2`（[#3124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3124)）**：`da-tools <cmd> | head -c1`、`… | true` 的讀端先離開、執行中 fd 1 被關，或 stdout 寫不進去（磁碟滿、`/dev/full`）時，先前不論工具原本要以 0、1、2 或 crash 結束，行程都以直譯器自己的 `120` 結束，輸出量大時還會被報成 `unexpected internal error`。現在一律以呼叫端錯誤 `2` 結束（`tenant-verify` 是 `1`），stderr 一行 `ERROR: stdout was closed before all output was written`（寫不進去時是 `ERROR: cannot write to stdout: <原因>`），沒有 traceback；工具在處理這個錯誤時以具名原因結束也一樣（工具自己指定結束碼時用它）；處理時 crash 或另報自己的錯誤，則維持該結束碼與它自己那一行，`raise … from` 另一個例外時亦同（[#3156](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3156)）。工具關掉 fd 1 後又開檔（或把檔案 `dup2` 到 fd 1）時，該檔保留工具自己寫的內容，失敗的 stdout 殘留的位元組也不會寫進去；stdout 有緩衝（未設 `PYTHONUNBUFFERED`）、fd 2 先被關、stdout 讀端先離開（EPIPE）時，由 `120` 改為 `2`（stderr 已關，沒有那一行）。只認 stdout 自己的寫入失敗：socket 或其他檔案的同類錯誤照舊。輸出在讀端關閉前已全部送出、或 shell 在啟動前就關掉 stdout（`>&-`）時，結束碼照舊；設了 `PYTHONUNBUFFERED=1` 時，工具自己吞掉的輸出失敗（例如 `--help`）不會被認出。CI 若用 `120` 判斷「讀端提前離開」，請改看 `2` 與這一行。
