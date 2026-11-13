---
section: Changed
topic: dx
issues: [3634]
created: 2026-11-13T10:03:38+00:00
---
- **fuse-commit 與 win-commit 的 hook 閘門：hook 沒過一律回 `2`（dx；[#3634](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3634)）**：git 與 pre-commit 會把 hook 的「發現」和「沒跑起來」（hook 回 2／3／127、找不到執行檔、設定錯誤）一律壓成 `1`，工具無法證明是「要改內容」，所以 `fuse_plumbing_commit.py --auto` 的 `git commit` 失敗與 `run_hooks_sandbox.sh` 的 hook 失敗都改回 `2`，hook 的輸出原樣轉印（⚠️ 結束碼變更：之前是 `1`）。另外，normal path 在 commit 前先確認 git 會跑的 pre-commit hook 存在、可執行（會考慮 `core.hooksPath`），且 pre-commit 啟動得了，否則回 `2`、不 commit；之前沒裝 hook 時直接 commit、回 `0`。
