---
section: Security
topic: vector
issues: [4164]
created: 2026-11-22T07:11:31+00:00
---
- **projection-gate 改以 tenant-api 自己的讀法判定 registry（vector chart 0.14.0；[#4164](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4164)）**：gate 原本以 Python 另讀 `_account_registry.yaml`，Go 讀者拒收的三種 registry（兩個租戶共用同一個 id、id 不小於 `next_account_id`、id 超過 uint32）它都放行。現在由新的 init-container（`projectionGate.registryReader.image`，tenant-api 的映像，版號隨 tenant-api 發布）執行 `tenant-api state show`，gate 只讀它印出的 JSON 並比對 projections；拒收、讀不到或未知格式都判 `registry_unreadable` 並降級。gate 映像 0.1.4 不再帶 YAML 讀者。`tenant-api state show/restore --from <目錄>` 也讀得到 ConfigMap 掛載（key 是 symlink）。⚠️ 需動作：重建並推送 gate 映像 0.1.4，並讓每個節點拉得到 tenant-api 映像。
