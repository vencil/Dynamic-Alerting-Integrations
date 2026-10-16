---
section: Fixed
topic: alertmanager-routing
issues: [2295]
created: 2026-09-28T12:59:42+00:00
---
- **Alertmanager 載入不了的 receiver 改為擋下，選填值與 `http_config` 由產生器與 da-guard 一起檢查（alertmanager-routing；[#2295](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2295)）**：Go 端規則收成 `pkg/receiverspec`，da-guard 與 exporter 共用。新增的檢查只擋 Alertmanager 會拒收的值：`send_resolved`／`require_tls` 須為 YAML 布林（`yes`／`off` 等可用，`y`／`n` 刻意擋）；`http_config` 的 auth 最多一種；token、`proxy_url` 與 `no_proxy` 須為字串（YAML 會讀成布林、數字或日期的值請加引號），`proxy_url` 能否解析由 da-guard 以 Go `net/url` 判、產生器交給 amtool；proxy 相關欄位不得互相矛盾。刻意比 Alertmanager 嚴格的形狀以共享表標 `strict` 的列為準。經 tenant-api 的寫入由產生器判（見 #2903 那一條）。
