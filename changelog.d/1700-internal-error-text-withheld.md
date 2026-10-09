---
section: Security
topic: tenant-api
issues: [1700]
created: 2026-10-09T16:11:38+00:00
---
- **伺服器內部錯誤不再把錯誤原文回給 caller（tenant-api；[#1700](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1700)）**：行為變更。`code` 為 `INTERNAL_ERROR` 的回應一律回固定訊息，原文（可能含 conf.d 絕對路徑、暫存檔路徑、git 訊息）只進 server log，用回應裡的 `request_id` 對照；只說明哪個操作失敗的少數固定訊息照常回傳。tenant 寫入（`PUT /tenants/{id}`、custom-alerts、batch 逐筆結果）的伺服器端失敗同樣改回固定訊息、status 不變，驗證與 merge 錯誤照常顯示原文。conf.d 解碼錯誤改用 `CONFIG_DECODE_ERROR`、PR 模式切不回 base 改用 `BASE_RESTORE_FAILED`（只列分支與是否已推送，不含 git 錯誤），兩者仍是 500。
