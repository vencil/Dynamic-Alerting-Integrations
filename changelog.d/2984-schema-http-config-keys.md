---
section: Fixed
topic: alertmanager-routing
issues: [2984]
created: 2026-10-15T16:33:18+00:00
---
- **validate-config 不再誤擋 Alertmanager 收的 `http_config` 鍵（alertmanager-routing；[#2984](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2984)）**：v2.9.0 的 `tenant-config.schema.json` 只列 `basic_auth`、`bearer_token`、`proxy_url`，receiver 寫 `proxy_from_environment`、`oauth2`、`tls_config` 等鍵時，路由產生器、da-guard 與 Alertmanager 都收，validate-config 卻報 `json_schema` FAIL。現在 schema 列的正好是 Alertmanager 的 `http_config` 鍵，`tls_config`、`oauth2`、`authorization`、`basic_auth`、`http_headers`、`proxy_connect_header` 的內層也照 Alertmanager 的規則檢查（未知鍵、互斥欄位、`oauth2` 缺 `client_id`／`token_url`、`authorization` 的 `type: basic`、保留的 header 名稱），鍵集合由測試對 Alertmanager 本身的 struct 釘住。內層目前只有 schema 判，路由產生器交給 `--validate` 的 amtool，da-guard 與 tenant-api 不判。`follow_redirects`／`enable_http2`／`insecure_skip_verify` 請寫不加引號的布林。
