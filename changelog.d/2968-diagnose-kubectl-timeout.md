---
section: Changed
topic: da-tools
issues: [2968]
created: 2026-10-14T13:03:30+00:00
---
- **`diagnose` 遇到 `kubectl` 沒回應時結束碼改為 `2`（[#2968](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2968)）**：Pod 檢查的 `kubectl` 超過 120 秒沒回應時，先前以 Python traceback 結束（結束碼 `1`，與「租戶有問題」無法區分）；現在與缺少 `kubectl` 同類，stderr 印一行 `ERROR:`、沒有 JSON、結束碼 `2`。已經找到問題後抓 log 的 `kubectl logs` 逾時則不影響結果：照常輸出問題、結束碼仍是 `1`，`recent_logs` 為空並以 `recent_logs_error` 註明原因。以結束碼 `1` 判斷「租戶不健康」的腳本，要把 `2` 當成「沒能檢查」處理。
