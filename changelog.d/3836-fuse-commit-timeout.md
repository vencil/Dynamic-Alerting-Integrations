---
section: Fixed
topic: dx
issues: [3836]
created: 2026-11-22T11:12:24+00:00
---
- **`make fuse-commit` 不再因 pre-commit 跑超過 60 秒而 traceback（dx；[#3836](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3836)）**：一般路徑的 `git commit` 會跑完整套 pre-commit hook，上限由 60 秒改為 30 分鐘；其他 git plumbing 步驟維持 60 秒。任何一步逾時都以結束碼 2 與一行具名說明結束。`git commit` 逾時時連同它的 hook 一起停止（先送 SIGINT，讓 pre-commit 還原暫存的未 stage 變更，再 SIGKILL；Windows 只停得到 git），並說明 commit 是否落地未知，要先用 `git log -1` 確認再重試。
