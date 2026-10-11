---
section: Fixed
topic: da-tools
issues: [2852]
created: 2026-10-11T08:36:03+00:00
---
- **`compile_custom_alerts` 與 `check_rulepack_sync` 只認自己所在的那棵樹（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：這兩支會隨 recipe-preview 映像出貨，原本以「第一個帶 `.git` 的祖先目錄」當根目錄。本樹沒有 `.git` 時（vendored、`git archive`），`check_rulepack_sync` 會比對外層專案的 rule pack，`compile_custom_alerts` 的預設 `--config-dir` 與 `--check` 比對對象也會落到外層。現在根目錄比照 #2845 取「包住工具的 `scripts/tools` 所在目錄」。不在任何 `scripts/tools` 底下時，`check_rulepack_sync` 一律回 rc 2；`compile_custom_alerts` 需要預設路徑的執行回 rc 2 並提示要明確給 `--config-dir` 與 `--out`，兩者都給則照常編譯。checkout 與 git worktree 的行為不變。
