---
section: Changed
topic: tools
issues: [1560]
created: 2026-11-04T23:46:27+00:00
---
- **⚠️ BREAKING：receiver 憑證欄位接上 Secret 參照，Secret 名稱由租戶 id 推導（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：webhook／Rocket.Chat `url`、Slack `api_url`、Teams `webhook_url`、PagerDuty `service_key`／`routing_key`、email `auth_password` 可寫成 `{secret: {key}}`（租戶自己的 `da-<tenant>`）或 `{secret: {shared: <別名>, key}}`（部署端 policy 新增的 `shared_secrets`）。產生器 render 成 `<field>_file: <root>/<secret-name>/<key>`，`<root>` 預設 `/etc/alertmanager/secrets`，用 `generate-routes --secret-mount-root` 改。對每個租戶的有效 receiver 判：別名不在 policy、欄位不在條目的 `fields`、`auth_password` 用共用 Secret、`auth_password` 用參照但 policy 沒有 `allowed_domains` 或 smarthost 不通過，該 receiver 都不產出。BREAKING：`definitions.secretRef` 不再收 `name`；`shared_secrets` 寫壞時整份 policy 不可用（結束碼 2）；repo 自帶的 `watchdog-heartbeat` 改掛到 `/etc/alertmanager/secrets/watchdog-heartbeat/`。自備 Alertmanager 必須照 `<root>/<secret-name>/<key>` 掛載，見 [BYO 整合指南 §5](docs/integration/byo-alertmanager-integration.md)。Operator 路徑是後續工作。
