---
section: Fixed
topic: tenant-api
issues: [1535]
created: 2026-10-17T19:01:51+00:00
---
- **tenant-api：commit 失敗回 500，groups／views 寫入失敗後讀取面立刻收斂（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：直寫模式的 `PUT /tenants/{id}` 與 `PUT /tenants/{id}/custom-alerts` 在 git commit 或寫檔失敗時，原本回 `400 BAD_REQUEST`，讓用戶端以為是 body 寫錯；現在回 `500 INTERNAL_ERROR`，訊息固定，細節只進 server log（以 `request_id` 對照）。body 本身被驗證擋下的，仍然是 4xx。請求在寫入完成前被取消或逾時則回 `503`（而非 `500 INTERNAL_ERROR`），與 batch 共用同一句訊息；寫入指標上，用戶端斷線不計入 `tenant_api_writes_total`，伺服器端逾時則照其他 503 計為 `result="error"`。這句訊息不再宣稱「什麼都沒寫入」：git 子程序可能已移動 ref 才被取消信號結束，commit 有可能已經落地，所以改成請用戶端先重新整理確認再重試。直寫 batch 被取消時的對外文字因此也改了：舊為 `the batch was cancelled before it was committed; nothing was written`，新為 `the write was cancelled or timed out before it finished; refresh to see whether it was committed before retrying`（同步回應的 `error` 與非同步 batch 逐項結果的 `message` 皆同）；比對舊字串的用戶端需要更新。非同步 batch 的逐項結果也改帶 `code: INTERNAL_ERROR`。另外，groups／views／federation 白名單的寫入不論成功或失敗都會重新載入記憶體快照：ConfigMap 更新已經生效、但用戶端收到 503 時，緊接著的 GET 就看得到這筆寫入，DELETE 也不會回 404。
