---
section: Fixed
topic: confd-family
issues: [2438, 2437]
created: 2026-09-30T00:34:28+00:00
---
- **da-guard 現在會執行經 YAML merge key 帶入的 domain policy，alias key 的名稱與產生器一致（[#2438](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2438)、[#2437](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2437)）**：`_domain_policy.yaml` 在 `domain_policies:` 之下以 `<<: *anchor` 帶入 domain、整個 policy 或 `constraints` 時，`generate-routes --strict` 會據以報錯，da-guard 先前卻不讀而結束碼 0；現在會照樣執行，明寫的鍵優先於 merge 帶入的鍵。`routing_profiles:` 之下以 `<<:` 帶入的 profile 也同樣生效。以 alias 當 key 的 profile／domain 名稱（`*p :`），da-guard 與 tenant-api 過去記成 anchor 名，租戶引用 anchored 文字時被報為 unknown profile；現在一律取 anchored 文字，與產生器相同。
