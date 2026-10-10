---
section: Changed
topic: tenant-api
issues: [1977]
created: 2026-10-10T02:46:45+00:00
---
- **`GET /tenants/{id}/effective` 不再每次重讀整棵 conf.d（tenant-api；[#1977](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1977)）**：每個請求仍自己走一次 conf.d，但以上一次完成的 walk 為 prior：mtime 沒動、且已超過 exporter mtime guard 的檔只 stat 不讀；之後只讀該租戶的檔、根目錄的 `_` 檔，以及從根到租戶目錄各層的 `_defaults.yaml`。回應（含 `merged_hash`、`not_served`、`chain_parse_failed`、overlay）與冷掃整棵樹相同。`PUT` 回應之後的 `GET` 看得到新值。這次 walk 改用寫入路徑同一套上限（預設 5 秒）與卡住偵測，卡住的 walk 不影響寫入。已知限制與寫入路徑相同：prior 記錄時還在 mtime guard 內的檔，若在同一個 mtime 刻度內被改寫成同樣大小，這次回應讀到的檔會發現並改走冷掃；其他檔則沿用 prior 記錄的租戶宣告（例如新出現的重複宣告看不到）。
