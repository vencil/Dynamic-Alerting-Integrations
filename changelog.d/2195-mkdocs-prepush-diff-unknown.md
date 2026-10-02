---
section: Fixed
topic: dx
issues: [2195, 2573]
created: 2026-09-27T17:30:00+00:00
---
- **mkdocs strict 的 pre-push 守衛對每個被推的 branch 都建站（dx；[#2195](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2195)、[#2573](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2573)）**：`scripts/ops/pre_push_mkdocs_strict.sh` 不再用路徑清單判斷「這次推送有沒有動到文件」。那份清單漏了 `rule-packs/`、`scripts/mkdocs/` 等建站實際讀的輸入，只改這些的推送會略過建站。現在只要推的是 branch 且帶著 commit，就對它的 tip 建站（裝了 mkdocs 時每推一個 branch 多一次建站的時間）；刪除、tag、`refs/notes/*` 不建，這支守衛也不再擋 notes 的推送。
