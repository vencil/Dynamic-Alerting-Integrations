---
section: Changed
topic: tools
issues: [1560]
created: 2026-11-07T09:44:26+00:00
---
- **⚠️ BREAKING：產生器拒收寫出來的 receiver 憑證（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：每個檔寫的原始值（含沒人綁的 profile、被蓋掉的 `_routing_defaults`）只收參照的欄位寫了值、webhook `url`／`proxy_url` 帶 userinfo、出現 `proxy_connect_header`，都是 `credential_plaintext`；`type` 不合法或 `overrides`／`routes` 不是 list 的 receiver 是 `credential_unjudged`。`generate-routes --validate` 結束碼 1、`validate-config` routes 列 FAIL、tenant-api 回 422 `GENERATOR_FINDINGS`（只擋這筆寫入改到的檔）；行內不印值。`shared_secrets.<別名>.fields` 改寫完整路徑（`http_config.basic_auth.password`；只寫 `http_config` 使 policy 不可用）。遷移：① `da-tools migrate-credentials`；② 照 [secret-leak-remediation-sop](docs/internal/secret-leak-remediation-sop.md) 輪替；③ `fields` 改完整路徑；④ 代理認證改由 Alertmanager 的 `HTTPS_PROXY`，receiver 寫 `proxy_from_environment: true`。tenant-api 的 log 對產生器 finding 只記結構化欄位，不記 stderr 與 message。見 [BYO §5](docs/integration/byo-alertmanager-integration.md)。
