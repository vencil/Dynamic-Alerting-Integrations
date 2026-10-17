---
section: Changed
topic: alertmanager-routing
issues: [2906]
created: 2026-10-17T07:31:00+00:00
---
- **⚠️ Breaking：`tenant-config.schema.json` 的布林欄位只收布林與 null，`_severity_dedup` 收產生器認得的每一種寫法（schema；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：`definitions.yamlBool`（`send_resolved`、`require_tls`、`proxy_from_environment`）拿掉 YAML 1.1 的字串 `"yes"`／`"on"`／`"true"` 等，與產生器只依型別判斷一致。`_severity_dedup`（租戶層與 `_defaults` 層）從只收 `enable`／`disable` 放寬為 `enable`、`enabled`、`on`、`true`、`disable`、`disabled`、`off`、`false`（不分大小寫、可前後空白）、布林與 null，即產生器與 exporter `ResolveSeverityDedup` 認得的值；詞表由測試對 `resolve.go` 釘住。
