---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-17T19:01:51+00:00
---
- **tenant-api：commit 失敗回 500，groups／views 寫入失敗後讀取面立刻收斂（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：直寫模式的 `PUT /tenants/{id}` 與 `PUT /tenants/{id}/custom-alerts` 在 git commit 或寫檔失敗時，原本回 `400 BAD_REQUEST`，讓用戶端以為是 body 寫錯；現在回 `500 INTERNAL_ERROR`，訊息固定，細節只進 server log（以 `request_id` 對照）。body 本身被驗證擋下的，仍然是 4xx。非同步 batch 的逐項結果也改帶 `code: INTERNAL_ERROR`。另外，groups／views／federation 白名單的寫入不論成功或失敗都會重新載入記憶體快照：ConfigMap 更新已經生效、但用戶端收到 503 時，緊接著的 GET 就看得到這筆寫入，DELETE 也不會回 404。
