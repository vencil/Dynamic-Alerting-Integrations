---
section: Fixed
topic: tenant-api
issues: [2830]
created: 2026-10-10T08:55:34+00:00
---
- **tenant-api 與 exporter 對 `_metadata` 的讀法一致（tenant-api；[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：tenant-api 的租戶清單與搜尋，以及它其他讀 `_metadata` 的地方，改用 exporter 讀 `/metrics` 的同一段程式。過去 exporter 讀得到、tenant-api 讀成空的三種寫法，現在 tenant-api 也讀得到：由 `_profile` 選用的 profile 帶入的 `_metadata`（租戶檔與平台檔都沒寫時）；寫成字串的 `_metadata`（`"environment: production\ndomain: finance\n"`）；值不是字串的純量（`environment: 123` 讀成 `"123"`）。讀法同樣跟著 exporter 的還有：`_metadata` 裡任一欄位依型別解不了時（例如 `environment` 底下是 mapping、`tags` 給單一字串），整份讀成空，過去 tenant-api 會保留其他欄位；`tags`／`groups` 清單裡的非字串項目轉成文字保留，過去會略過。規則見 [config-driven](docs/design/config-driven.md)。
