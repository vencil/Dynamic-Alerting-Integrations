---
section: Changed
topic: exporter
issues: [1425]
created: 2026-10-30T12:35:08+00:00
---
- **threshold-exporter chart：資源改落在 release namespace，新增 `thresholdConfig.existingConfigMap`（breaking，[#1425](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1425)）**：Deployment、Service、PDB、ConfigMap、ServiceMonitor 改用 `{{ .Release.Namespace }}`；以前寫死 `monitoring`，`helm install -n obs` 的資源仍落在 monitoring。⚠️ 遷移：以 `-n` 非 monitoring 安裝的既有 release，升級後資源搬進 release namespace：Helm 先在新 namespace 建立、最後才刪舊 namespace 那份，期間兩份短暫並存（SD 同時涵蓋兩邊時會短暫重複 series）。`prometheus.io/scrape` 的 SD namespace 清單要涵蓋新 namespace，`k8s/03-monitoring/network-policies.yaml` 裡釘在 monitoring 的兩條 threshold-exporter NetworkPolicy 也要在新 namespace 補上。`existingConfigMap` 設了名字時 chart 不建 ConfigMap、改掛那一份；`da-tools init --deploy kustomize` 的樹會自建 `threshold-config`，安裝 chart 時要帶 `--set thresholdConfig.existingConfigMap=threshold-config` 且 `-n` 與樹相同，init 的 README 與 Next steps 已寫明；這個開關從 2.9.0 之後的 chart 版本起才有。
