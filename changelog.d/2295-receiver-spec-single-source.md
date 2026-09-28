---
section: Fixed
topic: alertmanager-routing
issues: [2295]
created: 2026-09-28T12:59:42+00:00
---
- **tenant-api `PUT` 改擋壞掉的 receiver，選填值與 `http_config` 三方一起檢查（alertmanager-routing；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)）**：`PUT /api/v1/tenants/{id}` 以前對未知 type、缺必填、`ftp://` URL、PagerDuty 雙鍵、純量 receiver 一律回 200 並寫入。現在 body 自己寫的 receiver（主 `receiver`、`overrides[].receiver`、`routes[].receiver`）不合規時回 400 `INVALID_BODY`，每個問題一條 `violations[]`，直寫與 PR 模式都不寫入；從 profile／`_routing_defaults` 繼承的不判，domain policy 違規仍優先回 403。Go 端的 receiver 規則收成一份 `pkg/receiverspec`，da-guard、tenant-api 與 exporter 共用。另外三方（da-guard、路由產生器、schema）新增檢查：`send_resolved`／`require_tls` 必須是布林；`http_config` 的 auth 最多一種；`proxy_url` 必須是帶 host 的 http(s)／socks5 URL。產生器在 PATH 上沒有 amtool 時也會擋。
