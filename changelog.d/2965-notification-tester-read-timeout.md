---
section: Fixed
topic: alertmanager-routing
issues: [2965]
created: 2026-10-14T12:59:03+00:00
---
- **`test-notification` 把「連上了但回應太慢」正確回報為 `TIMEOUT`（[#2965](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2965)）**：receiver 接受連線、卻在 `--timeout` 內沒送回回應時，先前被標成 `CONNECTION_REFUSED`（https 的明細是 `The read operation timed out`），讓人往防火牆或埠號方向查。原因是 urllib 只把連線與 TLS 握手階段的逾時包成 `URLError`，等待回應階段的逾時直接以 `TimeoutError` 拋出。現在 http 與 https 的讀取階段逾時都歸為 `TIMEOUT`；包在 `URLError` 裡、訊息為空的逾時也依型別判定。連線被重置等其他網路錯誤不算逾時；`--ci` 的結束碼不變（所有錯誤狀態都算失敗）。
