---
section: Security
topic: security-supply-chain
issues: [1243, 1337]
created: 2026-09-28T11:26:22+00:00
---
- **第三方 image 升版與 digest 更新，清掉夜掃大半 fixable CVE（[#1243](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1243)）**：Prometheus → v3.15.0、Alertmanager → v0.34.1、Grafana → 12.4.12（12.4.11 清掉 kin-openapi CRITICAL，12.4.12 再清掉 libcrypto3／libssl3 與 thrift 兩筆 HIGH）、prom-label-proxy → v0.15.1、mysqld-exporter → v0.20.0、MariaDB → 11.8.9、oauth2-proxy → v7.15.4、prometheus-config-reloader → v0.94.1、kube-state-metrics → v2.20.0；alpine/git（清掉 openssh／perl CRITICAL）只換 digest（上游同 tag 重推）。
  `k8s/`、`helm/`、nightly 掃描 matrix、`try-local/` 與兩套測試 compose 一起對齊，同一元件在各部署面不再是不同版本（[#1337](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1337)）；部署精靈帶出的 Prometheus／Alertmanager tag 同步更新。
  ⚠️ mysqld-exporter v0.20.0 把每次 scrape 的預設連線上限由 1 調為 2；Alertmanager v0.34 的 `alertmanager_notifications_failed_total{reason}` 把 401/403、429 從 `clientError` 拆出來。kube-state-metrics v2.20 的 `kube_pod_status_reason` 只輸出實際設定的 reason（本 repo 規則未使用）。
