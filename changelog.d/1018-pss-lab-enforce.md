---
section: Changed
topic: k8s
issues: [1018]
created: 2026-11-06T02:40:00+00:00
---
- **實驗叢集的 namespace 全部 enforce Pod Security（k8s；[#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)）**：`k8s/00-namespaces` 的 `db-a`、`db-b`、`monitoring`、`tenant-api` 改為 enforce `restricted`，`vector` 維持 `privileged`，版本都釘在 kind node image 的 minor。`tests/pss` 守住三件事：每個 namespace 都 enforce、等級與同名 chart 宣告的一致、版本跟著 node image 走。`scripts/run_load.sh` 的 Job／Pod 與內部 runbook 的 `kubectl run` debug pod 補上 restricted 安全性設定，同樣由 `tests/pss` 判定。對外文件新增 namespace 的 Pod Security 設定建議。
