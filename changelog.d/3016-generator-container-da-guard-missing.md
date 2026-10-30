---
section: Added
topic: tenant-api
issues: [3016]
created: 2026-10-30T14:00:00+00:00
---
- **產生器容器找不到或無法執行 da-guard 時回 503（ADR-036 §3）**：與 amtool 相同，每次判定前先確認 `$DA_GUARD_BINARY`（未設時找 PATH）指到可執行、且回應得了 `--version` 的檔（檔案沒變就不重跑），否則回 503 `VALIDATOR_UNAVAILABLE`，`reason` 為 `da_guard_missing`；`GET /v1/status` 多帶 `da_guard_available`，並計入 `ready`。tenant-api 認得這個 `reason`，照原樣轉給呼叫者。
