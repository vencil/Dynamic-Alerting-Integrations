---
section: Fixed
topic: threshold-governance
issues: [2929]
created: 2026-10-13T12:54:41+00:00
---
- **`threshold-govern --max-prs`：dry-run 摘要計入上限，小於 1 時拒絕（[#2929](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2929)）**：先前上限只在 `--apply` 時讀取，dry-run 的 Summary 仍寫「全部可處理租戶都會開 PR」；現在上限小於可處理租戶數時改寫成 `with --max-prs N, at most N PR(s) would be opened`（dry-run 不查 tenant-api，所以是「最多」）。`--max-prs 0` 或負數先前把每個租戶都記成 skipped、一個 PR 都不開仍回結束碼 `0`，現在以結束碼 `2` 拒絕。
