---
section: Added
topic: ci
issues: [3468]
created: 2026-10-31T14:28:17+00:00
---
- **刻意付出的效能成本改寫進 `.github/bench-accepted.toml`，bench 閘門核對實測後不再開票（[ADR-039](docs/adr/039-bench-acceptance-ledger.md)；[#3468](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3468)）**：在引入成本的同一個 PR 新增一筆紀錄（benchmark 完整名稱、指標、上限、理由）。PR 上的 bench gate 與合併後的逐 commit 歸因只採用該次變更新增的紀錄：名稱與指標吻合且實測不超過上限時判為「已接受」，不貼 `perf-regression` label、不開票，只在執行摘要列出；超出上限仍是退化，並同時寫出宣告值與實測值。帳本解析失敗時判定步驟失敗；紀錄裡的 benchmark 名稱不存在時 CI 轉紅。過去只寫在 PR 說明裡的取捨，合併後的歸因讀不到，會照樣開票。
