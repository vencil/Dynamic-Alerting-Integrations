---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-26T02:42:04+00:00
---
- **`plan_race_shards merge --output` 與 `inject_default_key` 的寫入改經 `write_repo_artifact`（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：兩者都會被讀回（合併後的串流由 `timings --input` 與 `parse_go_test_json --input` 整份載入；`_defaults.yaml` 重跑時先讀回，由 exporter 以另一個 uid 載入），所以改為原子取代，寫入失敗時保留原檔。路徑不能用時，原本是 traceback 加 `unexpected internal error`（rc 2），現在是一行並點名 `--output` 或 `PATH`（rc 2）。`-` 原本會在 cwd 寫出名為 `-` 的檔（rc 0），現在 rc 2。`inject_default_key` 的目標是 FIFO 時原本會卡住，現在 rc 2；改寫既有檔時權限一律為 `0644`（原本沿用舊檔權限，例如 `0600`），與 conf.d 的規則（#3535）一致。正常輸出逐位元組不變。
