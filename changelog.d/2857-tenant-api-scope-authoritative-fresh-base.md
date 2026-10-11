---
section: Security
topic: tenant-api
issues: [2857]
created: 2026-10-11T02:40:00+00:00
---
- **PR 模式的 metadata 寫入範圍改由最新 base 權威判定（tenant-api；[#2857](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2857)）**：pod 本機樹只在啟動時同步，之前 PUT 的寫入前／寫入後範圍與 batch 的寫入前範圍都只在本機樹判，origin 上租戶或 profile 已改到別的 environment 時，enforce 下仍會開出 PR。現在本機樹上的判定只能提早拒絕；寫入鎖內、以 PR 分支所依據的最新 base 再判 pre-state 與 post-state（再判 domain policy，最後驗證），PUT 與 batch 皆然，不通過回 403 `FORBIDDEN`、不開 PR。這次判定一律執行；shadow 下只在最新 base 的 environment/domain 與本機判過的不同時多記一筆 `tenant_api_scope_would_deny_total{axis="metadata_write"}`，所以 soak 窗量得到這類寫入、且不重複計數。
