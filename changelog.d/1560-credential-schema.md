---
section: Added
topic: tenant-api
issues: [1560]
created: 2026-10-21T21:01:16+00:00
---
- **tenant-config schema 標出 receiver 憑證欄位，並預先定義 Secret 參照形態（schema；[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：webhook 的 `url`／`http_config`、Rocket.Chat 的 `url`、email 的 `auth_password`／`headers`、Slack 的 `api_url`、Teams 的 `webhook_url`、PagerDuty 的 `service_key`／`routing_key` 帶擴充關鍵字 `"x-credential": true`，新增測試要求這些欄位名都在遮罩名單內（遮罩仍依 key 名比對，不由 schema 推導）。新增 `definitions.secretRef`（`{secret: {name, key}}`，name 為 DNS-1123 subdomain、key 依 Secret data key 規則）；**目前尚未接到任何欄位**，租戶檔的寫法與驗證結果不變。Alertmanager 自己的 `*_ref`／`*_file` 欄位則改為拒收（[#3257](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3257)）。
