---
section: Changed
topic: tools
issues: [1560]
created: 2026-11-04T23:46:27+00:00
---
- **⚠️ BREAKING：receiver 憑證欄位接上 Secret 參照，Secret 名稱由租戶 id 推導（[#1560](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1560)）**：webhook／Rocket.Chat `url`、Slack `api_url`、Teams `webhook_url`、PagerDuty 兩 key、email `auth_password`、webhook `http_config` 的 `basic_auth.password`、`authorization.credentials`、`bearer_token`、`tls_config.key`、`oauth2` 的三個金鑰欄位、header 的 `secrets` 項目，可寫 `{secret: {key}}`（`da-<tenant>`）或 `{secret: {shared, key}}`（policy 新增的 `shared_secrets`）。產生器 render 成 Alertmanager 的 `*_file: <root>/<secret-name>/<key>`，`<root>` 預設 `/etc/alertmanager/secrets`（`--secret-mount-root`）。送往租戶自填目的地的欄位（`auth_password`、`http_config` 各欄）只收自己的 Secret，policy 要有 `allowed_domains` 且目的地明文通過，不得經 `proxy_url`；`tls_config.key` 以外另需 `follow_redirects: false`。違反則該 receiver 不產出。BREAKING：secretRef 不收 `name`；`shared_secrets` 寫壞時 policy 不可用（結束碼 2）；`watchdog-heartbeat` 改掛 `<root>/watchdog-heartbeat/`。掛載見 [BYO 整合指南 §5](docs/integration/byo-alertmanager-integration.md)。
