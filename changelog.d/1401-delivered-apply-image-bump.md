---
section: Security
topic: da-tools
issues: [1401]
created: 2026-09-28T11:26:30+00:00
---
- **`da-tools init` 產生的 GitLab 部署 image 升版（[#1401](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1401)）**：kustomize 路線的 `DA_KUBECTL_IMAGE` 由 `alpine/k8s:1.34.9` 升到 `1.34.12`、helm 路線的 `DA_HELM_IMAGE` 由 `alpine/helm:3.21.3` 升到 `3.22.0`（仍在 Helm 3 線上）。只影響之後新產生的專案；既有專案的 CI 變數不會自動改變，需要重跑 `da-tools init` 或自行改那個變數。
