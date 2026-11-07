---
section: Fixed
topic: da-tools
issues: [3477]
created: 2026-11-01T23:20:20+00:00
---
- **maintenance-scheduler 推送 metrics 遇到非 HTTP 回應時不再 crash（ops；[#3477](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3477)）**：`--pushgateway` 指向回應不是 HTTP 的服務（含回應被截斷、header 過長或過多）時，原本以 `internal_error` 結束（rc 2），而 silence 其實已建立；現在與其他推送失敗（連不上）一樣只印 WARN，rc 由 silence 的結果決定（0 或 1）。URL 本身不合法（沒有 scheme、埠不是數字等）不在此列：解析參數時就以 rc 2 擋下（[#3528](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3528)）。「傳輸失敗」的例外清單集中在 `_lib_prometheus.TRANSPORT_ERRORS`。**Breaking**：該情況 rc 2 → 0／1，`--json-output` 改為輸出正常文件。
