---
section: Changed
topic: da-tools
issues: [2932]
created: 2026-10-13T12:48:42+00:00
---
- **`da-tools init --deploy helm` 產生的 helm 部署步驟固定 chart 版本（[#2932](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2932)）**：GitHub workflow 與 GitLab pipeline 的 `helm upgrade --install … oci://ghcr.io/vencil/charts/threshold-exporter` 一律帶 `--version`，值為執行 `init` 那一版對應的 threshold-exporter chart 版本（`exporter/v*` 版號線，以 `helm/threshold-exporter/Chart.yaml` 為準），不再在 job 執行當下浮動到 registry 上最新的 chart。要升 chart 請直接改產生物裡那一行的 `--version`。既有專案不受影響；portal 的 CI/CD 設定精靈預覽同步帶 `--version`。
