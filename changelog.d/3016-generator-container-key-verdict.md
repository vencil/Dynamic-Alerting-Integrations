---
section: Added
topic: tenant-api
issues: [3016]
created: 2026-10-27T08:00:00+00:00
---
- **產生器容器在放行的寫入之後附上鍵的判決（tenant-api；ADR-038 決定 5）**：`POST /v1/validate` 的 `pass` 回應多帶 `key_verdict`，是被寫的檔寫入前後宣告的租戶的共用判決（`judge_tree --tenant`）。判決只回報，不改變 `result`、不拒收寫入；它有自己那一份期限（判定期限前 2 秒停），時間或記憶體不夠、da-guard 失敗時回 `not_completed` 與原因（`no_time`、`deadline`、`memory`、`error`）；有另一筆寫入在排隊時立刻讓位（`preempted`）。寫到 defaults 檔或帶 `profiles:` 的根平台檔時回 `platform_file`，交給整棵樹的判決（PR 的 CI）。新增 metric `da_generator_key_verdicts_total{outcome}` 與 `da_generator_key_verdict_seconds`。tenant-api 目前不讀這個欄位，寫入的 API 回應不變。判決成本隨樹成長（1 CPU 上約 1000 個租戶 1.2 秒、2500 個 3 秒、5000 個 6 秒），約 2500 個租戶以上通常回 `not_completed`。
