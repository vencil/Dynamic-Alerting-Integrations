---
section: Changed
topic: da-tools
issues: [3393, 3276]
created: 2026-10-31T03:17:30+00:00
---
- **⚠️ Breaking：`tenant-verify` 的結束碼改為與其他工具相同的 0／1／2（[#3393](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3393)）**：先前 `1` 與 `2` 的意義對調。現在 `1` = 驗證失敗（`--expect-merged-hash` 不一致、tenant 不存在、重複宣告，`--all` 遇到重複宣告），先前是 `2`；`2` = 什麼都沒驗（參數錯、conf.d 不存在或讀不了、da-guard 不可用或拒收、整棵樹因另一個 tenant 重複宣告而被拒、stdout 寫不進去、crash），先前是 `1`；argparse 的參數錯照舊是 `2`。帶 `--json` 時，沒有自己文件的 `2` 會在 stdout 印一份共用的 `caller_error` envelope（先前是空的）。**升級時請更新 Emergency Rollback checklist 第 6 項**（[incremental-migration-playbook](docs/scenarios/incremental-migration-playbook.md)）與照它寫的腳本：舊版「exit 2 = 不一致」會把新的 `1`（不一致）當成「不是 2」而跳過回滾；新的 `2` 表示指令或環境錯誤，修正後重跑，不得據此回滾。
