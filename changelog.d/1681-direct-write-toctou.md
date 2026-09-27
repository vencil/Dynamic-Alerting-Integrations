---
section: Fixed
topic: tenant-api
issues: [1681]
created: 2026-09-27T11:51:04+08:00
---
- **直寫路徑的落點解析與驗證改在寫入鎖內做判定（tenant-api；[#1681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1681)）**：`PUT /tenants/{id}`（`Write` / `WriteIfUnchanged`）過去在排隊拿寫入鎖**之前**就解析 tenant 檔並跑完驗證，排在前面的寫入改動 conf.d 之後仍依舊判定行事。三筆並行請求因此可能各自回 200，卻留下兩個檔宣告同一個 tenant id、exporter 拒收整棵樹；反方向則會把落地時合法的寫入誤拒（409 / 400），過期 base 的 `WriteIfUnchanged` 也拿到 400 而非「請重新讀取」的 precondition 錯誤。現在排隊前只檢查 body 本身的格式（與 PR 模式的 pre-flight 同一支），讀樹的判定全部在拿鎖後、對實際落地的樹做。代價是這些判定改為持鎖執行，大型 body 或大型 conf.d 會讓排在後面的其他 tenant 寫入等更久。
