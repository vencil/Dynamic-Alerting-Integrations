---
section: Changed
topic: tenant-api
issues: [2486, 2730]
created: 2026-10-08T15:34:20+00:00
---
- **domain policy 檔存在卻不能用時，直寫模式的 policy 判定寫入回 503（tenant-api；[#2486](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2486) Q7-2）**：`_domain_policy.yaml`／`.yml` 是目錄、dangling symlink、讀不了或解析失敗，且該檔沒有自己上一份讀得了的內容時，`PUT /tenants/{id}`、`POST /tenants/batch` 與 `POST /groups/{id}/batch` 中寫 `_routing_receiver_type` 或碰 `_routing`／`_routing_profile` 的請求回 503 `POLICY_UNAVAILABLE`（帶 `Retry-After`，不寫入；async 批次執行時逐筆再判）。先前這些情況（含啟動時就壞、熱重載新增壞檔、改成 dangling symlink）會當成沒有 policy 而放行。其他寫入與讀取不受影響，`/ready` 維持 200。新增 gauge `tenant_api_policy_available`、counter `tenant_api_policy_unavailable_open_total`（每放行一次寫入計一次：一個 PUT 或 batch 中一個 op）、告警 `TenantApiPolicyUnavailable`，以及逃生開關 `--policy-unavailable-open`（`TA_POLICY_UNAVAILABLE_OPEN`；helm `policy.unavailableOpen`）。PR 模式判 base 時，目錄與 dangling symlink 也算讀不了。
