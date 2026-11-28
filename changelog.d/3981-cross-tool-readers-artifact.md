---
section: Changed
topic: dx
issues: [3981]
created: 2026-11-28T10:50:50+00:00
---
- **被另一支工具讀回的輸出改為 repo 生成物（⚠️ breaking；[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`run_chaos_soak --output-dir`（由 `render_soak_diff --input-dir` 讀）、`parse_go_test_json --output`（由 `file_race_report` 讀）與 `trufflehog_to_sarif --output`（由 upload-sarif 上傳）改走原子取代：寫入失敗時舊檔保留，不再留下半份或空檔；FIFO、指向 stdout 的路徑一律 rc 2。`run_chaos_soak` 的兩個檔改為 0644，且整段 soak 結束時才一次寫出（被 SIGKILL 的 soak 不留任何資料）；`summary.txt` 移除，摘要改印在 stderr。`parse_go_test_json` 寫不出時由 traceback 改為 rc 2 一行。
