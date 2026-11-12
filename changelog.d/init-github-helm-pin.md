---
section: Fixed
topic: da-tools
issues: []
created: 2026-11-07T14:00:00+00:00
---
- **`da-tools init --deploy helm` 產生的 GitHub workflow 改為自己安裝釘版的 helm（da-tools）**：apply job 原本直接用 runner 預裝的 helm，`ubuntu-latest` 正從 Ubuntu 24.04（Helm 3.22.1）分批換成 26.04（Helm 4.3.1）（2026-10-19 開始，GitHub 預定 11-19 完成），被換到的 runner 上客戶沒改任何東西就跨到 Helm 4。現在先以 `azure/setup-helm@v5` 安裝與 GitLab 路線 `DA_HELM_IMAGE` 同一版（3.22.1）再執行 `helm upgrade`，兩條路線的版本只有一個來源（`init_project.py` 的 `HELM_VERSION`）。⚠️ 已產生的專案不會自動更新，也不要靠重跑 `init`（已初始化的專案會被拒絕，`--force` 會覆寫你改過的檔案）：請在 apply job 的 `actions/checkout` 之後自行加上 `- uses: azure/setup-helm@v5`、`with: {version: v3.22.1}`。Helm 3 的安全修補到 2027-02-10，轉到 Helm 4 會在那之前另行公告。
