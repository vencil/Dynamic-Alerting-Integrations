---
section: Added
topic: log-federation
issues: [609, 908]
created: 2026-09-26T17:00:00+00:00
---
- **⚠️ Vector 租戶日誌投影加上防洩漏 gate（`helm/vector`）**：新增 `projection-gate` init-container，在 Vector 啟動前比對 `tenantProjections` 與 conf.d 的 `_account_registry.yaml`，抄錯 `accountId` 就不放行該租戶的 sink。預設 `degrade`（退回平台-only `0:0`、Vector 照常運作），`enforce` 則讓 pod 直接失敗。另有 gate 判定 metric 與三條 critical 告警（mismatch、開機時 registry 讀不到、init 卡住）、以 Warn／Audit 偵測 gate 被移除的 `ValidatingAdmissionPolicy`（需 K8s ≥1.30），以及 gate 容器改以非 root、drop ALL 執行。⚠️ 升級：已設 `tenantProjections` 的多租戶安裝須先提供 `projectionGate.registry.configMapName`，否則 `helm upgrade` 會被擋下；從 0.8.0（含）以前的版本以 `--reuse-values` 升級也會被擋，需補上 securityContext 或不帶 `--reuse-values`。單租戶安裝不受影響（[#908](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/908)）。
