---
section: Removed
topic: planning
issues: [1741, 2106]
created: 2026-11-13T09:30:00+08:00
---
- **⚠️ breaking：`check_trk_index_coverage.py` 不再掃 commit 歷史，`--surface` 移除，pre-commit hook `trk-index-coverage` 刪除（[#1741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1741)、[#2106](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2106)）**：舊的 commits 掃描面用 `%(trailers)` 讀 main 歷史，而 squash merge 會拆散 trailer block（`dev-rules.md` §P1 規定 main 上不這樣讀）；它只掛在 commit 之前，看不到正在寫的那顆，違規一旦進了 main 又無從修正。偵測改由 issue／PR 標題面負責（`trk-index-coverage.yaml`），表的凍結照舊由 Python Tests 驗。遷移：`--surface titles --title …` 改成 `--title …`，`--surface titles`／`all` 的全量掃描改成 `--all-titles`；不帶掃描旗標時只驗表本身。
