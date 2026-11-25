---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-25T21:04:13+00:00
---
- **`parse_go_test_json --output`、`plan_race_shards`（`timings --output`、`fetch-timings --out-dir`）、`bench_synth_tenants --state-file` 的寫入改經 `write_operator_output`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：輸出路徑寫不了時，結束碼原本就是 `2`，但 `parse_go_test_json` 與 `plan_race_shards` 印的是 traceback 加 `unexpected internal error`；現在是一行並點名旗標，控制字元會被跳脫。`bench_synth_tenants` 寫不了時仍不注入任何租戶。`--output -` 改為寫到 stdout（原本會寫出一個名為 `-` 的檔），此時那行 receipt 改印到 stderr；`--state-file -` 結束碼 `2`、不注入，因為 `cleanup` 讀不回。寫出的檔（含覆寫自有的既有檔）權限改為 `0600`，原本照 umask 或維持既有權限。
