---
section: Changed
topic: dx
issues: [2852]
created: 2026-11-21T23:35:52+00:00
---
- **⚠️ breaking：`scan_component_health`、`check_pipestatus_errexit`（未給 `--root` 時）與 rule-tree 合約（`_rule_tree`）只在本樹自己就是 git 工作樹根時執行（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：三者原本以「最近的 `.git` 祖先」當根，本樹被 vendor 進別的 repo 時，`git ls-files`／`git log` 讀到的是外層 repo。現在先取包住工具的 `scripts/tools` 所在目錄，再要求 `git rev-parse --show-toplevel` 指向同一個目錄（新增 `_lib_compat.require_git_project_root`；比對前移除 hook 匯出的 `GIT_DIR` 等變數），不符、git 失敗或不在 git 底下一律 rc 2 並點名兩個根。一般 clone 與 git worktree 不受影響；`check_pipestatus_errexit --root` 照舊不檢查。
