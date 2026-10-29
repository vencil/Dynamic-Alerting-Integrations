---
section: Changed
topic: da-tools
issues: [3276]
created: 2026-10-29T00:48:54+00:00
---
- **呼叫端給的路徑改由同一個判定回答（ops 工具；[#3276](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3276)）**：`--config-dir`、`--dir`、`--policy`、`--dashboard-dir` 等呼叫端給的路徑，位於執行者進不去的上層之下時，ops 工具不再以 traceback（`--json` 的 reason 為 `internal_error`）或「not found」結束，而是指名旗標、路徑與原因，結束碼仍為 2。`--json` 的 reason 改為 `dir_not_found`／`dir_not_accessible`／`file_not_found`／`file_not_accessible`／`path_not_found`／`path_not_accessible`。**Breaking**：policy-engine 的 `config_dir_not_found`、`policy_file_not_found` 改為 `dir_not_found`、`file_not_found`；其他工具原本為通用的 `caller_error`。
