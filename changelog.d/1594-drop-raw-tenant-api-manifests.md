---
section: Removed
topic: tenant-api
issues: [1594]
created: 2026-10-21T15:00:00+00:00
---
- **⚠️ BREAKING：刪除 tenant-api 的原生 K8s manifests，`helm/tenant-api` 成為唯一部署來源（k8s；[#1594](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1594)）**：那套 manifests 沒有任何流程在 apply，傳給 binary 的 `--state-store`／`--state-configmap` 連它自己釘的 v2.7.0 都不認得，也沒有設 write mode。`tenant-api` Namespace（含 PSS label）移到 `k8s/00-namespaces/namespace-tenant-api.yaml`，因此 `scripts/setup.sh` 會在 kind 開發環境建出它、`cleanup.sh` 會連同其中的 helm release 一起刪掉。da-portal 與 recipe-preview 的 `networkPolicy.egress.tenantApiPodSelectors` 預設拿掉 `app: tenant-api`，只留 chart 設的 `app.kubernetes.io/name: tenant-api`；自管 manifests 部署的 tenant-api 要帶這個 label。threshold-govern 的 egress 改選 `app.kubernetes.io/name`（舊 selector 選不到 chart 部署的 pod）；chart 預設 RBAC 沒有 `threshold-governance` 群組，要自行加進 `rbac._rbacYaml`。
