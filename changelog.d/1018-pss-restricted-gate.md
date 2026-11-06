---
section: Added
topic: helm
issues: [1018]
created: 2026-11-06T02:10:00+00:00
---
- **CI 以上游 Pod Security Admission 判定每個 chart 渲染出的 pod（chart；[#1018](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1018)）**：新的 `tests/pss` 以 `k8s.io/pod-security-admission` 判定每個 chart（預設值與 `ci/*.yaml`）和 `k8s/` manifest，版本與本地 kind 叢集一致。除 `helm/vector`（`Chart.yaml` 新增 `dynamic-alerting.io/pod-security-level: privileged`，須裝在獨立的 privileged namespace）外，預設設定都符合 `restricted`。⚠️ 已知例外：tenant-api 的 `confDir.type=hostPath` 過不了 restricted（連 baseline 也不行），不能裝在 enforce restricted 的 namespace。判定不到的範圍（列舉分支、執行期建立的 pod、mutating webhook）見 `docs/internal/pss-enforcement-runbook.md` §0.1。
