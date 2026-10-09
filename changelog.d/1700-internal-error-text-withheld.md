---
section: Security
topic: tenant-api
issues: [1700]
created: 2026-10-09T16:11:38+00:00
---
- **伺服器錯誤不再把錯誤原文回給 caller（tenant-api；[#1700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1700)）**：行為變更。`code` 為 `INTERNAL_ERROR` 的回應一律回固定訊息，原文（可能含 conf.d 絕對路徑、暫存檔路徑、git 訊息）只進 server log，用回應裡的 `request_id` 對照。tenant 寫入的伺服器端失敗（`PUT /tenants/{id}`、custom-alerts、batch 逐筆結果）同樣改回固定訊息、status 不變；驗證、merge、租戶檔歸屬不明的錯誤照常顯示原文。metric discovery 的 502 與 federation 准入檢查的警告原因不再帶上游 Prometheus 的 URL／IP。設定內容解碼失敗（`/effective`、custom-alerts、`_groups.yaml`／`_views.yaml`）改用 `CONFIG_DECODE_ERROR`，PR 模式切不回 base 改用 `BASE_RESTORE_FAILED`（只列分支與是否已推送），兩者仍是 500。
