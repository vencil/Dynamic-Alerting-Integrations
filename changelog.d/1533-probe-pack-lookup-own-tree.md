---
section: Fixed
topic: alertmanager-routing
issues: [1533]
created: 2026-10-10T16:40:00+08:00
---
- **平台告警 pack 只在本樹的位置找，不再採用別的 repo 的 pack（`generate-routes`；[#1533](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1533)）**：原本工具從所在目錄往上找第一個有 `.git`／`Makefile`／`pyproject.toml` 的目錄，取它底下的 `k8s/03-monitoring/`。這棵樹被放進另一個 repo、或只把 `scripts/` 丟到別的 checkout 底下時，會靜默採用對方的 pack（實測只剩 1 筆、沒有 WARN），外層沒有 pack 時又會漏掉這棵樹自己的。現在只看兩處：映像裡工具旁邊，以及本樹根目錄的 `k8s/03-monitoring/`（根目錄以「`scripts/tools/ops/` 底下就是這支工具本身」認定）。兩處都沒有就是找不到，走既有的降級警告。
