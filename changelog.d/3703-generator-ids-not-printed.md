---
section: Changed
topic: tools
issues: [3703]
created: 2026-11-13T20:00:00+00:00
---
- **⚠️ BREAKING：route generator 的訊息與 finding 不再帶作者寫的 id、名字（[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：不是名稱形（`[A-Za-z0-9_.-]{1,64}`）的值只說長度（`a string of N chars`）；拿來定位的名字（租戶 id、profile／policy 名、路徑裡的鍵）印成替身 `<key:3f9a1c>`（UTF-8 的 SHA-256 前 6 個 hex：同名同替身，兩個名字是兩筆 finding），訊息裡加引號。比對輸出者注意：`invalid_tenant_id` 的訊息改以宣告它的檔開頭（`a.yaml: tenant id '…'`）並帶 `file`，`tenant` 可能是 null；`duplicate_tenant`、`severity_dedup_invalid` 的 `tenant` 可能是 null；`routing_in_unread_location`／`invalid_receiver_field`／`credential_unjudged` 的 `field` 變成 `profiles.<key:…>.…`／`defaults.<key:…>`，訊息寫 `` `_profile: <key:…>` ``；`routing_profile_duplicate`、`domain_policy_unusable`、`domain_policy_out_of_scope` 的 routing profile／domain policy 名字也一樣；平台檔 `tenants:` 裡沒有租戶檔宣告的 id 那行 WARN 變成 `tenants.<key:…> ignored — no tenant file declares tenant '<key:…>'`；body 不是 mapping 的租戶那行 WARN 同樣不印 id；`dead_entry` 的值不是名稱形時只說長度。conf.d 檔名、目錄名（含 `file`）逐段依 ConfigMap key 規則：合法照印（如 tenant-api 的 `<id>.yaml`），不合法印替身。
