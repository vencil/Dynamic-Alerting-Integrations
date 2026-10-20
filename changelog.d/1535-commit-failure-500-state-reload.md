---
section: Fixed
topic: tenant-api
issues: [1535, 3173]
created: 2026-10-17T19:01:51+00:00
---
- **tenant-api：commit 失敗回 500，取消與 git 逾時依結果回 503（[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)、[#3173](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3173)）**：直寫 `PUT /tenants/{id}` 與 `.../custom-alerts` 的 commit 或寫檔失敗原回 `400`，現回 `500 INTERNAL_ERROR`，細節只進 log；body 被驗證擋下的仍是 4xx。請求取消或逾時，或 git 逾時（`TENANT_API_GIT_TIMEOUT`）而重讀分支確認沒動，回 `503` `…nothing was written, so it is safe to retry`；推進分支回錯又讀不回分支時回 `503`「`it may have been committed — refresh …`」，單租戶與 batch 都保留 publish marker，下次寫入或重啟讀得到分支時收尾；單租戶直寫在寫檔前就記下 marker，中途失敗沒能放回的檔照 HEAD 放回，收尾完成後不再卡 `TENANT_FILE_DIRTY`。同步與非同步 batch（逐項 `message`、task `error`）同句，舊的 `the batch was cancelled before it was committed; nothing was written` 不再出現，比對字串的用戶端須更新；非同步逐項結果帶 `code: INTERNAL_ERROR`。用戶端斷線不計入 `tenant_api_writes_total`，伺服器端逾時照其他 503 計為 `result="error"`。groups／views 結果不明的失敗在背景重載快照，最遲下個 WatchLoop 週期；確定沒寫入的不重載。
