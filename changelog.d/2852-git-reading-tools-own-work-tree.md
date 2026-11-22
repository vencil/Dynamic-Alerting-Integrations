---
section: Changed
topic: dx
issues: [2852]
created: 2026-11-21T23:35:52+00:00
---
- **⚠️ breaking：`scan_component_health`、`check_pipestatus_errexit`（未給 `--root` 時）與 rule-tree 合約（`_rule_tree`）只讀本樹自己的 git（[#2852](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2852)）**：三者原本以「最近的 `.git` 祖先」當根，本樹被 vendor 進別的 repo 時會讀到外層 repo。現在先取包住工具的 `scripts/tools` 所在目錄，要求 `git rev-parse --show-toplevel` 指向同一個目錄；環境中繼承來的 `GIT_DIR`、`GIT_INDEX_FILE`、`GIT_WORK_TREE` 也必須屬於本樹（本樹的 git dir／其中的 index／本樹的根），外層 repo 的 hook 匯出的這類變數會被拒絕。任一不符、git 失敗或不在 git 底下都回 rc 2，並點名本樹的根與不符之處（新增 `_lib_compat.require_git_project_root`）。本樹自己的 clone、worktree，以及本樹自己 commit hook 帶的變數（含 `commit -a`）照常執行；`GIT_CONFIG*`（例如由環境帶入的 `safe.directory`）會保留。`check_pipestatus_errexit --root` 照舊不檢查。
