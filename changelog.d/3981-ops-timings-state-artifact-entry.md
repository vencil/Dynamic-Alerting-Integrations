---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T21:04:13+00:00
---
- **`plan_race_shards`（`timings --output`、`fetch-timings --out-dir`）與 `bench_synth_tenants --state-file` 的寫入改經 `write_repo_artifact`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：這些檔會被程式整份讀回（`plan --timings-dir`、`cleanup`），所以改為原子取代，寫入失敗時保留舊檔。路徑寫不了時，結束碼原本就是 `2`，但 `plan_race_shards` 印的是 traceback 加 `unexpected internal error`；現在是一行並點名旗標，控制字元會被跳脫。`bench_synth_tenants` 寫不了時仍不注入任何租戶。指定 `-` 時，原本會在 cwd 寫出一個名為 `-` 的檔，現在結束碼 `2`（`--state-file -` 不注入）。
