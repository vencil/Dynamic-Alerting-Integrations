---
section: Fixed
topic: da-tools
issues: [1501]
created: 2026-09-28T08:14:36+00:00
---
- **da-tools 映像內 `threshold-recommend --generate-observed-map` 不再把 observed-map 靜默清成 0 筆（[#1501](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1501)）**：`_observed_map_lib` 與 `_registry_lib` 原本從模組位置往上數三層找 repo，映像把工具攤平到 `/opt/da-tools/` 後算成 `/`，於是找不到任何 rule pack、把每個既有條目都當成「已移除」丟掉，且 rc 0。現在改成先找模組旁邊隨映像出貨的 rule pack、再往上找專案根目錄；兩處都找不到時以 rc 2 報錯並列出找過的位置。`_registry_lib` 只在 repo 內才有意義的函式（讀寫 threshold registry、重生 generated surfaces）在映像內改為明確報錯，而不是去開 `/rule-packs/…`。
