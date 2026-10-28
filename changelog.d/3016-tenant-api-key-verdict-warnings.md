---
section: Added
topic: tenant-api
issues: [3016]
created: 2026-10-28T08:00:00+00:00
---
- **寫入回應附上鍵的判決（tenant-api；ADR-038 決定 5）**：產生器容器放行一筆寫入時回的鍵判決，以 `key verdict:` 開頭的行加進寫入回應的 `warnings`（`PUT /tenants/{id}`、`PUT /tenants/{id}/custom-alerts`、`POST /tenants/batch`、`POST /groups/{id}/batch`，直接寫入與 PR 模式、同步與非同步都有）。判決只回報、不擋寫入。揭露沿用寫入回應既有的規則：被寫入檔案裡、寫入對象租戶的 finding 一律顯示；同檔其他租戶的只在呼叫者能讀該租戶時顯示；平台檔、profile 或沒被寫到的檔案上的只給能看整棵樹的呼叫者，其他人只看到一行「有看不到的 finding，細節在伺服器日誌」（非同步批次的任務結果一律如此）。判決未完成時只寫原因，不轉述容器的訊息；容器沒回判決（舊映像）時寫「未完成（not reported）」，不會看起來像沒找到問題。直接寫入的批次把租戶的行掛在該租戶第一個有變更的操作上、整筆的行只掛一次，判決未完成則每個有變更的操作都寫；每份清單最多 100 行（寫入對象租戶的行優先），其餘以一行計數；產生器沒送完的 finding 數只給能看整棵樹的呼叫者。判決看的是寫入之後的樹、不與寫入前比較；內容沒變的寫入沒有判決。`WriteValidator.ValidateWrites` 改為回傳 `(*KeyVerdict, error)`（內部介面）。
