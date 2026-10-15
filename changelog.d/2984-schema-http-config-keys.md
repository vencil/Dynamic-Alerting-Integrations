---
section: Fixed
topic: alertmanager-routing
issues: [2984]
created: 2026-10-15T16:33:18+00:00
---
- **validate-config 不再誤擋 Alertmanager 收的 `http_config` 鍵（alertmanager-routing；[#2984](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2984)）**：`tenant-config.schema.json` 的 `httpConfig` 以前只列 `basic_auth`、`bearer_token`、`proxy_url`，receiver 寫 `proxy_from_environment`、`oauth2`、`tls_config` 等鍵時，路由產生器、da-guard 與 Alertmanager 都收，validate-config 卻報 `json_schema` FAIL。現在 schema 列的正好是 Alertmanager 的 `http_config` 鍵（測試以反射對 Alertmanager 本身的 struct 釘住），其他鍵照樣擋；五種 auth 任兩種並用、proxy 欄位互相矛盾也由 schema 擋，判定與產生器、da-guard 一致。`follow_redirects`／`enable_http2` 請寫不加引號的布林。
