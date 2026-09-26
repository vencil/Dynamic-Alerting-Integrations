---
section: Changed
topic: gitops-onboarding
issues: [1349, 1350, 1351, 1426, 1794, 1797, 1827, 1828, 1829, 2043]
created: 2026-09-26T17:00:00+00:00
---
- **GitOps 交付的破壞性變更（`da-tools init`、組裝工具、da-guard）**：⚠️ `da-tools init --config-source git`（GitOps Native Mode）與 `--deploy argocd` 撤下並以 exit 2 拒絕——前者產出的 patch 打不到任何目標，後者的管線以出貨狀態不可能成功；ArgoCD 使用者請把自己的 Application 指向 `--deploy kustomize` 產生的樹（[#1349](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1349)、[#1351](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1351)）。⚠️ 帶 `--ci`／`--rule-packs`／`--deploy` 卻沒給 `--tenants` 時不再自動補上範例租戶 `db-a,db-b`：終端機下改為補問，CI／腳本中 rc 2（[#1426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1426)）。⚠️ `configmap-assemble` 對 repo 自帶範例樹硬擋（需 `CONFDIR=` 或 `ALLOW_SAMPLE_CONFDIR=1`），檔名不是合法 ConfigMap key 即 rc 1，只差大小寫的同租戶載體改為兩者都選入而被判重複；`sharded-assemble` 遇到跨檔重複租戶改為拒絕。⚠️ da-guard 未帶 `--cardinality-limit` 時改讀根 `_defaults.yaml` 的 `max_metrics_per_tenant`（未設即 500），要維持舊行為請傳 `--cardinality-limit 0`。
