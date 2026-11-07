---
section: Fixed
topic: da-tools
issues: []
created: 2026-11-07T14:00:00+00:00
---
- **`da-tools init --deploy helm` 產生的 GitHub workflow 改為自己安裝釘版的 helm（da-tools）**：apply job 原本直接用 runner 預裝的 helm，`ubuntu-latest` 在 2026-10-19～11-19 從 Ubuntu 24.04（Helm 3.22.1）換成 26.04（Helm 4.3.1），客戶沒改任何東西就跨到 Helm 4。現在先以 `azure/setup-helm@v4` 安裝與 GitLab 路線 `DA_HELM_IMAGE` 同一版（3.22.1）再執行 `helm upgrade`，兩條路線的版本只有一個來源（`init_project.py` 的 `HELM_VERSION`）。⚠️ 已產生的專案不會自動更新：請重新執行 `da-tools init`，或在 workflow 的 apply job 自行加上這個步驟。Helm 3 的安全修補到 2027-02-10，轉到 Helm 4 會在那之前另行公告。
