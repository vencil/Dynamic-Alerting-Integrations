---
section: Fixed
topic: da-tools
issues: [3739]
created: 2026-11-11T15:41:07+00:00
---
- **⚠️ Breaking：generator-service `--repo` 只收 repo 的根目錄，並由 git 判定、點名（ops 工具；[#3739](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3739)）**：`--repo` 給 work tree 裡的子目錄，先前會被當成外層 repo、以空的 conf.d 照常服務（每筆寫入都對著空樹判定），現在不啟動、結束碼 2；不是 repo 時，錯誤改為點名 `--repo <path>`，不再只有 git 原文。本服務執行的 git 一律去掉 `git rev-parse --local-env-vars` 列出的環境變數（只帶設定的 `GIT_CONFIG*` 保留，`safe.directory` 仍可經環境變數設定），先前繼承的 `GIT_DIR`（git hook 會設）會蓋過 `--repo`、改讀另一個 repo。bare repo 與 linked worktree 照常可用。
