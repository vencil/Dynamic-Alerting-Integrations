---
section: Fixed
topic: portal
issues: [2947]
created: 2026-11-14T16:50:44+00:00
---
- **部署精靈產生的 `helm upgrade --install` 帶上確切的 `--version`（portal；[#2947](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2947)）**：Deployment Profile Wizard 給的安裝指令原本沒有 `--version`，照抄會裝到 registry 當下最新的 chart。現在 threshold-exporter 與 tenant-api 兩條指令都釘已發布的 chart 版本；版號放在 `platform-data.json` 頂層的 `charts`，精靈讀它，離線時用內建的同一組。這組版號只在發版收尾時由 `make platform-data RELEASE_CHARTS=1` 從 `Chart.yaml` 重寫，`make pre-tag` 新增 `wizard-chart-pins-released`，擋下釘到沒有任何 tag 會發布的版本。版號不是單一確切版本（範圍、`latest`、空字串）或精靈要裝的 chart 沒有登記時，精靈直接報錯，不產生指令。
