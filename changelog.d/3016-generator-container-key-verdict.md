---
section: Added
topic: tenant-api
issues: [3016]
created: 2026-10-27T08:00:00+00:00
---
- **產生器容器在放行的寫入之後附上鍵的判決（tenant-api；ADR-038 決定 5）**：`POST /v1/validate` 的 `pass` 回應多帶 `key_verdict`，是被寫的檔寫入前後宣告的租戶的共用判決（`judge_tree --tenant`；`disable` 的反事實重算也只算這些租戶）。判決目前只回報，不改變 `result`、不拒收寫入。它與產生器共用同一次判定的期限，沒有自己的期限、不因有寫入在排隊而讓位（判定期限前 2 秒停，讓結果送得回去）；時間或記憶體不夠、da-guard 失敗時回 `not_completed` 與原因（`no_time`、`deadline`、`memory`、`error`），沒讀到任何根平台檔時回 `undecidable`。寫到 defaults 檔或帶 `profiles:` 的根平台檔時回 `platform_file`，交給整棵樹的判決（PR 的 CI）。`error` 級的 finding 一律送回，`truncated` 只計略去的 warning 與 notice。判定程序跑的 served-values 與 effective 也帶 `GOTRACEBACK=crash`，Go 自己的記憶體用盡記成 `memory`，不再是 `error`。新增 metric `da_generator_key_verdicts_total{outcome}`、`da_generator_key_verdict_seconds`，以及 `da_generator_judgement_deadline_ratio`（最近一次判定的耗時與時間預算之比；超過 0.5 該告警，到 1 時寫入會判不完）。tenant-api 把它附在寫入回應的 `warnings`（見「寫入回應附上鍵的判決」那一條）。判決成本隨樹成長（1 CPU 上約 1000 個租戶 1.2 秒、2500 個 3 秒、5000 個 6 秒），它佔掉的是寫入自己的時間。
