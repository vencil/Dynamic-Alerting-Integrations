---
section: Fixed
topic: tenant-api
issues: [2405]
created: 2026-09-29T14:07:59+00:00
---
- **整檔 `PUT` 能修回解析不了的租戶檔（tenant-api；[#2405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2405)）**：租戶檔 YAML 語法錯、`tenants:` 不是 mapping 或是 list、有重複 key 時，`PUT /api/v1/tenants/{id}` 送一份合法內容也一律回 400（`cannot read current custom alerts`），檔案只能直接改 git。原因是 end-of-life recipe 守衛要讀舊檔的用量當基線，讀不出就整個擋。現在讀不出的基線當成**零**：body 不含 end-of-life recipe 就放行、覆蓋修好，含了照樣擋——不會因為舊檔壞了就放寬。舊檔存在但讀取失敗（權限、I/O）仍拒絕寫入。壞的共用檔經此修回時無法保留其他租戶的區塊（帶上會被拒），需要保留請直接改 git。
