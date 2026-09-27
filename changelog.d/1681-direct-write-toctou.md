---
section: Fixed
topic: tenant-api
issues: [1681]
created: 2026-09-27T11:51:04+08:00
---
- **直寫路徑的落點解析與驗證改在寫入鎖內做最終判定（tenant-api；[#1681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1681)）**：`PUT /tenants/{id}`（`Write` / `WriteIfUnchanged`）過去在排隊拿寫入鎖**之前**就解析 tenant 檔並跑完驗證，排在它前面的寫入改動了 conf.d 之後，它仍依舊判定 commit。三筆並行請求因此可能各自回 200，卻留下兩個檔宣告同一個 tenant id、exporter 拒收整棵樹（一筆先移除共用檔裡的 section、一筆再替該 id 建立自己的檔、第三筆依舊基準把 section 寫回共用檔）。現在鎖外那一次只做提早拒絕，拿鎖後會對實際落地的樹重新解析與驗證，以這次為準——與 PR 模式已採用的形狀一致。代價是驗證改為持鎖執行，大型 body 會讓排在後面的其他 tenant 寫入等更久；上限仍由 `TA_MAX_TENANT_DOC_BYTES` 控制。
