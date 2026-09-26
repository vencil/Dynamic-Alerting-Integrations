---
section: Fixed
topic: helm-deployment
issues: [2027, 2044, 2073, 2075]
created: 2026-09-26T17:00:00+00:00
---
- **chart values 說到做到（helm）**：多個「宣告了卻沒有 template 讀」的 key 不再是 silent no-op。da-portal / tenant-api 的 `ingress.*` 現在真的產生 Ingress（預設關閉；後端固定走 oauth2-proxy，`oauth2Proxy.enabled=false` 或 `ingress.hosts` 為空時 render 直接失敗，[#2027](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2027)）。⚠️ threshold-exporter chart 移除從未生效的 `rules.operator.ruleLabels`／`receiverTemplate`／`secretRef`（改用 `da-tools operator-generate` 對應旗標，[#2073](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2073)），da-portal 移除從未被消費的頂層 `nginx:` 區塊；覆寫檔殘留這些 key 不會出錯，一如以往沒有作用。chart README 不再列不存在的 key。`operator-generate` 產出的 PrometheusRule／ServiceMonitor 預設帶 kube-prometheus-stack 需要的 selector label，新增 `--selector-label` 與 chart 的 `rules.operator.serviceMonitor.labels`（[#2075](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2075)）。
