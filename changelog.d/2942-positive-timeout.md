---
section: Fixed
topic: da-tools
issues: [2942, 2957]
created: 2026-10-13T16:38:02+00:00
---
- **`threshold-govern`、`notification-tester` 的 `--timeout` 必須大於 0（[#2942](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2942)、[#2957](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2957)）**：`0` 會讓連線立刻失敗、負數會在送出請求時丟例外。先前 `threshold-govern --apply --timeout 0` 每個 tenant-api 呼叫都失敗，回報成系統性失敗（結束碼 `1`）；`notification-tester --timeout 0` 把正常的 receiver 報成 `CONNECTION_REFUSED`，負數直接崩潰。現在 `0`、負數、`nan`、`inf` 與大於 `9223372036` 秒（會在連線時溢位崩潰）的值，在解析參數時就以結束碼 `2` 拒絕，並接受小數秒。`migration-audit --timeout`、`patch-config --reload-timeout`／`--poll-interval` 改用同一份檢查：差別只在大於 `9223372036` 的值改為拒絕（先前會在等待時溢位崩潰）。
