---
section: Changed
topic: da-tools
issues: [3124, 3156]
created: 2026-10-17T07:49:03+00:00
---
- **⚠️ 結束碼變更：寫 stdout 失敗時由 `120`／crash 改為 `2`（[#3124](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3124)、[#3558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3558)）**：`da-tools <cmd> | head -c1`、`… | true` 的讀端先離開、stdout 寫不進去（磁碟滿、`/dev/full`）或被關閉（`>&-`、執行中關掉 fd 1）時，先前行程以直譯器自己的 `120` 結束，輸出量大時還會被報成 `unexpected internal error`；`>&-` 則丟掉輸出、照舊結束碼。現在一律以 `2` 結束、永不為 `0`，stderr 一行 `ERROR: cannot write to stdout: <原因>`（`tenant-verify` 也是，[#3393](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3393)）；啟動前被關閉的 fd 0／1／2 以唯讀 `/dev/null` 補上，`2>&-` 不再讓 stderr 的訊息混進 stdout。`patch-config` 的寫入結果例外：照印那一行，結束碼保留 ConfigMap 實況的碼。已知限制見 dev-rules §13（argparse 吞掉的寫入錯誤、`PYTHONUNBUFFERED=1` 的部分寫入、工具自己吞掉的錯誤、Go 透傳的碼、stderr 那一行可能歸錯原因）。CI 若用 `120` 判斷「讀端提前離開」，請改看 `2`。
