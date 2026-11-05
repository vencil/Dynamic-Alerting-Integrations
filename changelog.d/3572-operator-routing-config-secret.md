---
section: Changed
topic: alertmanager-routing
issues: [3572]
created: 2026-11-05T14:40:00+08:00
---
- **⚠️ BREAKING：Operator 路徑不再產生 AlertmanagerConfig，路由改由 `generate-routes` 產出、經 Alertmanager CR 的 `configSecret` 載入（[#3572](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3572)）**：Prometheus Operator 把 AlertmanagerConfig 的路由插在全域路由之前、強制 `continue: true`、預設加 `namespace` matcher，平台路由排不到最前面、租戶 custom alert 隔離不成立。`operator-generate` 的 `--components` 只剩 `all`（= rules + servicemonitor）／`rules`／`servicemonitor`，不再讀 conf.d；`--components alertmanager` 與 `--config-dir`、`--api-version`、`--receiver-template`、`--secret-name`、`--secret-key` 結束碼 2，JSON `summary` 沒有 `alertmanager_configs`。`migrate-to-operator` 同樣不產 AlertmanagerConfig、不讀 conf.d、拒收這些旗標。改用 `generate-routes --output-alertmanager --base-config <你的設定>`，輸出以 key `alertmanager.yaml` 放進 Secret、設 `spec.configSecret`（kube-prometheus-stack：`alertmanagerSpec.useExistingSecret` + `configSecret`）。舊 CR 以 `kubectl delete alertmanagerconfig -n monitoring -l app.kubernetes.io/part-of=dynamic-alerting` 刪除；`operator-check` 第 4 項對殘留的 CR warn。
