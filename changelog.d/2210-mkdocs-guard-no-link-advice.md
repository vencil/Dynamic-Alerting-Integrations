---
section: Fixed
topic: dev-workflow
issues: [2210]
created: 2026-09-29T13:43:23+00:00
---
- **mkdocs strict 的 pre-push 守衛不再把每一種建站失敗都說成連結錯誤（internal、dx；[#2210](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2210)）**：`scripts/ops/pre_push_mkdocs_strict.sh` 以前只要建站回非 0，就印「Common fixes」連結修法和 bypass 建議。現在不給建議，只點名沒通過的 commit 並停在那一顆，不再接著建其餘的 ref。暫存 worktree 建不起來時，原樣印出 git 的錯誤，不再猜測原因、建議 `git worktree prune`。
