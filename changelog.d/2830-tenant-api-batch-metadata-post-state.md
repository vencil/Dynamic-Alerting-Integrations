---
section: Fixed
topic: tenant-api
issues: [2830]
created: 2026-10-10T11:37:36+00:00
---
- **batch 寫到 `_profile`／字串 `_metadata` 時，以寫入後的 metadata 判定寫入範圍（tenant-api；[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：#2830 讓 `_profile` 與字串形式的 `_metadata` 決定租戶的 environment／domain，而這兩個都是 `POST /tenants/batch`、`POST /groups/{id}/batch` 可寫的純量鍵。寫到這兩個鍵的 op 除了照舊以租戶現況判定，還會以寫入後的 metadata 再判定一次（PR 模式先疊上同一租戶在本批中較早的 op），與整檔 `PUT` 的 body 判定相同。開了 `--rbac-metadata-write-scope-enforce` 時，限定在 production 的呼叫者無法用 batch 把租戶改到 dev，該筆回 `error`、不寫入；shadow 模式行為不變。
