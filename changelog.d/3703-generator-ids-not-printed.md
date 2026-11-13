---
section: Changed
topic: tools
issues: [3703]
created: 2026-11-13T20:00:00+00:00
---
- **⚠️ BREAKING：route generator 的訊息與 finding 不再帶作者寫的 id、名字（[#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703)）**：不是名稱形（`[A-Za-z0-9_.-]{1,64}`）的一律只說長度（`a string of N chars`），路徑裡以 `<key>` 代替。比對輸出的腳本要知道：`invalid_tenant_id` 的訊息改以宣告它的檔開頭（`a.yaml: tenant id …`）並帶 `file`，`tenant` 可能是 null；`duplicate_tenant`、`severity_dedup_invalid` 的 `tenant` 可能是 null；`routing_in_unread_location`／`invalid_receiver_field`／`credential_unjudged` 的 `field` 變成 `profiles.<key>.…`／`defaults.<key>`，訊息寫 `` `_profile: a string of N chars` ``；平台檔 `tenants:` 裡沒有租戶檔宣告的 id 那行 WARN 變成 `tenants.<key> ignored — no tenant file declares tenant a string of N chars`；body 不是 mapping 的租戶那行 WARN 同樣不印 id；`dead_entry` 的值不是名稱形時只說長度；`_routing_profile` 指到不存在的 profile 時多列 `; available: …`。
