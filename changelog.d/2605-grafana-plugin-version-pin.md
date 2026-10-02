---
section: Security
topic: security-supply-chain
issues: [2605]
created: 2026-10-01T22:14:27+00:00
---
- **Grafana 的 VictoriaLogs datasource plugin 改成釘版本安裝（`k8s/03-monitoring/deployment-grafana.yaml`；[#2605](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2605)）**：原本的 `GF_INSTALL_PLUGINS=victoriametrics-logs-datasource` 沒有版本，每次開機都裝 grafana.com 上的最新版，而且這個變數在 Grafana 12.x 已 deprecated。現在改成 `GF_PLUGINS_PREINSTALL_SYNC=victoriametrics-logs-datasource@0.32.0`，版本固定、不會自動升級，由 Renovate 追 grafana.com 的版本另開 PR。另外以 `GF_PLUGINS_DISABLE_PLUGINS` 關掉 Grafana 12.4 預設在背景安裝、沒釘版本的 4 個 app plugin（Logs／Profiles／Traces／Metrics Drilldown）；本 repo 的 dashboard 沒用到它們，有自訂 dashboard 依賴它們的部署，需要自行改回。
