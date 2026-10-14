---
section: Fixed
topic: da-tools
issues: [2966]
created: 2026-10-14T13:15:48+00:00
---
- **等待秒數類參數在解析時就檢查範圍（[#2966](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2966)）**：`test-notification --rate-limit`、`threshold-govern --throttle-seconds`、`validate --interval` 接受 `0`（不等待）與小數，負數、`nan`、`inf` 改回結束碼 `2`；先前負數被靜默略過或在第一輪後崩潰，`threshold-govern` 遇到 `inf` 是在**開出第一個 PR 之後**才崩潰、也沒有報表。`baseline --interval`／`--duration` 必須是正整數秒：`--interval 0` 原本崩潰（結束碼 `1`），負數靜默產出空 CSV（結束碼 `0`），現在都回 `2`。federation revocation reconciler 的 `--interval`（chart 的 `reconcile.intervalSeconds`）必須大於 `0`：`0` 原本讓 daemon 忙迴圈查詢 VictoriaLogs，現在啟動即以 `2` 退出。`check_image_refs_resolve.py --timeout` 必須是正整數，`0` 不再把每個 image ref 判成逾時。
