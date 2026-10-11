---
section: Fixed
topic: da-tools
issues: [2852]
created: 2026-10-11T08:39:28+00:00
---
- **沒有自己 `.git` 的 da-tools 副本直接執行 `entrypoint.py` 時，不再執行外層專案的工具（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：`entrypoint.py` 旁邊找不到工具時，會改到 repo 的 `scripts/tools/` 底下找。原本以「第一個帶 `.git` 的祖先目錄」當 repo 根目錄，本樹沒有 `.git` 時（vendored、`git archive`），會執行外層專案的同名工具。現在根目錄依結構判定：`entrypoint.py` 位於 `components/da-tools/app` 時取往上三層，其他位置不再往外找，找不到工具就照既有方式回 rc 2。da-tools 映像、checkout 與 git worktree 的行為不變。
