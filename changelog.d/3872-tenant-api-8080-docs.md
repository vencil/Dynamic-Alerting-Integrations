---
section: Fixed
topic: docs
issues: [3872]
created: 2026-11-14T02:00:00+00:00
---
- **tenant-api 8080 的說明改成 ADR-027 之後的行為（docs；[#3872](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3872)）**：Helm chart 的 ingress 模板與 values 註解、tenant-api README、operator-prometheus 整合文件（中英）與一支測試的說明仍寫「8080 信任注入的身分 header」。ADR-027 之後 8080 只有 `/health`、`/ready`、`/metrics`，帶身分 header 回 400，API 只在 pod 內的 Unix socket 上、經 oauth2-proxy 進來。改寫這些敘述；結論不變：NetworkPolicy 必須開、Ingress 不指向 8080。行為沒有變更。
