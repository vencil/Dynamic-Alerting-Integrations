---
section: Changed
topic: alertmanager-routing
issues: [3522]
created: 2026-11-01T23:18:06+00:00
---
- **Alertmanager v0.34.2 起拒絕 host 帶第二個冒號的 URL，平台的判定跟著改（[#3522](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3522)）**：v0.34.2 改以 Go 1.26 預設建置，`http://proxy.example:80:3128/`、`http://::1/` 這類 `proxy_url` 會讓 Alertmanager 載入設定失敗；v0.34.1 仍接受。`da-read alertmanager` 與 receiver 判定表改為拒絕，與部署版本一致。⚠️ 升到 v0.34.2 前，請把這類 `proxy_url` 改成單一 port 的寫法，IPv6 位址要用方括號（`http://[::1]/`）。另外 `tests/alertmanager-inhibit` 的 Alertmanager module 由 v0.33.1 對齊到部署的 v0.34.2，並新增守衛擋版本漂移。
