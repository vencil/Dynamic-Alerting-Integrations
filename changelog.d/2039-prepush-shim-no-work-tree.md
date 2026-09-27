---
section: Fixed
topic: dev-workflow
issues: [2039]
created: 2026-09-27T14:05:48+08:00
---
- **pre-push shim 在沒有工作樹時不再叫你 rebase（internal、dx；[#2039](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2039)）**：從 bare repo 推，或在別的目錄用 `git --git-dir=… push` 推的時候，shim 找不到 dispatcher，以前一律說「這棵樹早於 #1689，請 rebase」，照做也修不好。現在訊息會印出它去找的那個工作樹路徑，並分別列出「舊 checkout → rebase」與「沒有工作樹 → 到 worktree 裡推」兩條出路。推送照樣會被擋，exit code 不變。重新安裝一次（`bash scripts/ops/install_prepush_hook.sh`）才會換上新的訊息。
