---
section: Changed
topic: da-tools
issues: [2995]
created: 2026-10-15T23:27:09+00:00
---
- **`test-notification` 新增 `CONNECTION_ERROR` 與 `TLS_ERROR` 兩種狀態，`CONNECTION_REFUSED` 只留給真的被拒絕連線（[#2995](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2995)）**：先前 DNS 解析失敗、連線被重置、伺服器收下請求後不回應就關線、TLS 憑證錯誤全部標成 `CONNECTION_REFUSED`，讓人往防火牆或埠號方向查。現在依例外型別分類：TLS 錯誤（含憑證驗證失敗、https 連到純 http 埠）標成 `TLS_ERROR`，其他連線錯誤標成 `CONNECTION_ERROR`。經 HTTPS proxy 時 CONNECT 失敗（`Tunnel connection failed`）標成 `CONNECTION_ERROR`，detail 前綴 `via proxy: `，表示測到的是 proxy。`--ci` 的結束碼不變；比對 `--json` 裡 `status` 字串的外部腳本需要更新。
