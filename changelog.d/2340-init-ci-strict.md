---
section: Changed
topic: da-tools
issues: [2340]
created: 2026-10-02T01:31:41+00:00
---
- **`da-tools init` 產生的 CI 與 pre-commit 範本改跑 `generate-routes --validate --strict`（[#2340](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2340)）**：GitHub Actions、GitLab CI 與 `.pre-commit-config.da.yaml` 三處都加上 `--strict`，新專案預設 strict。既有專案的範本只在 `init --force` 時重寫、不受影響；要手動啟用，在這三個檔案的 `generate-routes ... --validate` 後面加上 `--strict`。strict 會讓下列情況報 `ERROR` 並失敗：domain policy（ADR-007）違規、未加引號而被讀成非字串的 `routes[].match` 值或 `overrides[].alertname`／`metric_group`（#2431）、不合規的 `group_by` 元素（#2503）、既不是 mapping 也不是停用字串的 `_routing`、不是 mapping 的 `_routing_defaults`，以及不合法的租戶 id（#2341）。CI 精靈的預覽同步更新。
