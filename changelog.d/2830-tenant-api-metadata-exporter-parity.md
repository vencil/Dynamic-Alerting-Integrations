---
section: Fixed
topic: tenant-api
issues: [2830]
created: 2026-10-10T08:55:34+00:00
---
- **tenant-api 與 exporter 對 `_metadata` 的讀法一致（tenant-api；[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：tenant-api 的租戶清單、搜尋與單一租戶的讀取，改用 exporter 讀 `/metrics` 的同一段程式讀 `_metadata`。過去 exporter 讀得到、tenant-api 讀成空的三種寫法，現在 tenant-api 也讀得到：租戶檔不寫、由 `_profile` 選用的 profile 帶入的 `_metadata`；寫成字串的 `_metadata`（`"environment: production\ndomain: finance\n"`）；值不是字串的純量（`environment: 123` 讀成 `"123"`，過去 tenant-api 讀成空、exporter 讀成 `"123"`）。規則見 [config-driven](docs/design/config-driven.md)。
