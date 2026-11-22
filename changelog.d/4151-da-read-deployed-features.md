---
section: Added
topic: tools
issues: [4151]
created: 2026-11-22T12:33:28+00:00
---
- **da-read 的 rule parser 與部署的 Prometheus 用同一組 `--enable-feature`（tools；[#4151](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4151)）**：Prometheus 的 PromQL parser 由 `--enable-feature` 決定接受哪些語法；da-read 是離線讀客戶規則，讀不到部署 manifest，以前只能寫死「不開任何選項」。現在改由 `components/da-read/prometheus_features.go` 的 `deployedFeatures` 建 parser，並新增 `da-read prometheus-features -`，回答每個選項對 da-read 讀取的影響：改變 parser、不影響，或 da-read 沒有模擬（例如 `type-and-unit-labels`）。CI 自動找出 repo 內每個 Prometheus（`prom/prometheus`／`prometheus/prometheus` 映像，含 registry 前綴與 Helm 的 `{repository, tag}` 寫法，以及 Operator 的 `kind: Prometheus`），任一個的選項與 da-read 不一致、或用到 da-read 沒有模擬的選項時都會擋；Prometheus 升版多出新選項時，Go 測試直接讀 Prometheus 自己的 `setFeatureListOptions` 對帳，要人決定新選項的歸類。
