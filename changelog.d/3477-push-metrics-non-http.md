---
section: Fixed
topic: da-tools
issues: [3477]
created: 2026-11-01T23:20:20+00:00
---
- **maintenance-scheduler 推送 metrics 遇到非 HTTP 回應時不再 crash（ops；[#3477](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3477)）**：`--pushgateway` 指向回應不是 HTTP 的服務時，原本以 `internal_error` 結束（rc 2），而 silence 其實已建立；現在與其他推送失敗一樣只印 WARN，rc 由 silence 的結果決定（0 或 1）。「傳輸失敗」的例外清單集中在 `_lib_prometheus.TRANSPORT_ERRORS`。**Breaking**：該情況 rc 2 → 0／1，`--json-output` 由 envelope 改為正常文件。
