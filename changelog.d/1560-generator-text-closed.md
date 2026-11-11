---
section: Changed
topic: tools
issues: [1560]
created: 2026-11-10T12:46:03+00:00
---
- **產生器與 tenant-api 不再轉印作者寫的鍵與容器回的字串（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：finding 與鍵的判決行裡，不是名稱形的鍵改以遮罩顯示；憑證欄位的形狀讀不了時判為 `credential_unjudged`（`credential_not_value`），`migrate-credentials` 一併列出；`_routing_profile` 指向不存在的 profile 改用 kind `unknown_routing_profile`；`--strict` 判 `_routing_enforced` 時採用與 render 相同的 `--policy`。tenant-api 產生器拒收的回應不再指向 server log。
