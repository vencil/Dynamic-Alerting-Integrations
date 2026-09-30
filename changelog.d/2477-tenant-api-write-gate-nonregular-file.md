---
section: Fixed
topic: tenant-api
issues: [2477]
created: 2026-09-30T12:14:53+00:00
---
- **conf.d 裡租戶檔是非一般檔案（例如 FIFO）時，寫入請求不再停在權限檢查無法完成（tenant-api；[#2477](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2477)）**：寫入平面的權限檢查會讀租戶檔取 `_metadata` 的 environment／domain，先前用一般的讀檔，遇到具名管道會一直等下去，`PUT /api/v1/tenants/{id}` 等經過這道檢查的寫入因此無法完成。現在改用讀取平面同一支讀檔（非阻塞開檔、先確認是一般檔案），讀不到就照原本的規則當成未標記租戶。之後的寫入流程照既有的 conf.d 掃描上限處理，直寫模式回 500、不寫任何東西。
