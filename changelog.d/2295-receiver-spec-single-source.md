---
section: Fixed
topic: alertmanager-routing
issues: [2295]
created: 2026-09-28T12:59:42+00:00
---
- **tenant-api 改擋 Alertmanager 載入不了的 receiver，選填值與 `http_config` 三方一起檢查（alertmanager-routing；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)）**：`PUT /api/v1/tenants/{id}` 以前對未知 type、缺必填、`ftp://` URL、PagerDuty 雙鍵、純量 receiver 一律 200 並寫入。現在 body 自己寫的 receiver 不合規時回 400 `INVALID_BODY` 加 `violations[]`，直寫與 PR 模式都不寫入；`POST /{id}/validate` 判定一致。繼承來的不判，policy 違規仍先回 403。Go 端規則收成 `pkg/receiverspec`，da-guard、tenant-api 與 exporter 共用。三方新增的檢查只擋 Alertmanager 會拒收的值：`send_resolved`／`require_tls` 須為 YAML 布林（`yes`／`off` 等可用，`y`／`n` 刻意擋）；`http_config` 的 auth 最多一種；token 與 `proxy_url` 須為字串，`proxy_url` 能否解析由 da-guard／tenant-api 以 Go `net/url` 判、產生器交給 amtool；proxy 相關欄位不得互相矛盾。刻意比 Alertmanager 嚴格的形狀以共享表標 `strict` 的列為準。
