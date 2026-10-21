---
section: Fixed
topic: tenant-api
issues: [3206]
created: 2026-10-21T08:32:22+00:00
---
- **tenant-api 拒收寫入時附的產生器 finding，依檔名判斷所屬租戶改用共用的檔名規則，判不出租戶的檔一律不給受限呼叫者（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)）**：只對看得到整棵樹的呼叫者以外的人生效。指名檔案的 finding 現在依 exporter 與 da-guard 共用的規則取檔名的租戶：副檔名大小寫不拘（`Secret.YAML` 是租戶 `Secret` 的檔，過去不算，看不到 `Secret` 的人也收得到，現在隱藏），只去掉最後一個副檔名（`a.yml.yaml` 是租戶 `a.yml`，過去當成 `a`：看得到 `a.yml`、看不到 `a` 的人過去收不到，現在收得到）。檔名判不出租戶的——`_` 開頭的平台檔（例如 `_defaults.yaml`，過去照給）、隱藏或只有副檔名的名字（`.yaml`、`.x.yaml`）、不是 YAML 的檔——現在都不給受限呼叫者，計入被保留的筆數。
