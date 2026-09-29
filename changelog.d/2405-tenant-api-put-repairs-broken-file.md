---
section: Fixed
topic: tenant-api
issues: [2405]
created: 2026-09-29T14:07:59+00:00
---
- **整檔 `PUT` 能修回解析不了的租戶檔（tenant-api；[#2405](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2405)）**：租戶檔 YAML 語法錯、`tenants:` 不是 mapping 或是 list、有重複 key 時，`PUT /api/v1/tenants/{id}` 送一份合法內容也一律回 400（`cannot read current custom alerts`），檔案只能直接改 git。原因是 end-of-life recipe 守衛要讀舊檔的用量當基線，讀不出就整個擋。現在對**所有租戶都有寫入權限**的呼叫者（RBAC 規則 `tenants: ["*"]`，且不帶 `org-scope`、`environments`、`domains`；前綴樣式如 `svc-*` 不算）可以整份覆蓋修回，舊檔基線當成**零**：body 不含 end-of-life recipe 就放行，含了照樣擋。其他呼叫者照舊回 400、檔案不動，訊息指明需要全租戶寫入權限，否則直接改 git；`POST /tenants/{id}/validate` 的判定跟著呼叫者走。舊檔存在但讀取失敗（權限、I/O）仍拒絕寫入。
