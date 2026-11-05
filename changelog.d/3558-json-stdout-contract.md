---
section: Changed
topic: da-tools
issues: [3558, 3518, 3554]
created: 2026-11-05T00:00:00+00:00
---
- **⚠️ Breaking：`--json` 的 stdout 只有工具自己的文件；`>&-` 由 `0` 改為 `2`（[#3558](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3558)）**：stdout 非空，當且僅當工具交出文件（`Report`），此時恰好一份 JSON。呼叫端錯誤與 crash 只寫 stderr、以非零結束，stdout 是空的，不再補一份 `caller_error`／`internal_error` envelope。消費端先看結束碼：結束碼屬於該命令帶文件的碼，且 stdout 可 parse，文件才算數。啟動前被關掉的 fd 0／1／2 會以唯讀 `/dev/null` 補上，所以 `tool >&-` 有輸出時就是寫入失敗，結束碼 `2`；`2>&-` 不再讓 stderr 的訊息混進 stdout。`_lib_exitcodes` 移除 `arm_crash_envelope`／`disarm_crash_envelope`、`exit_caller_error(fields=)` 與 `STDOUT_CLOSED_MESSAGE`；新增 `Report(..., delivery="best_effort")`，目前只給 `patch-config` 用。
