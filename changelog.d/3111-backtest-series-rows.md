---
section: Changed
topic: threshold-governance
issues: [3111]
created: 2026-10-17T10:55:19+00:00
---
- **`backtest` 以 `/metrics` series 為單位回報；`--git-diff` 移除（[#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)）**：⚠️ 行為與結束碼變更。`--git-diff` 改為結束碼 2（`git_diff_removed`），訊息寫明改用：自己把 PR 的 base checkout 到另一個路徑（本機 `git worktree add /tmp/base HEAD~1`，CI 用 `actions/checkout` 取 base），再 `--config-dir conf.d --baseline <base 的 conf.d>`。每列是一條 series：只換寫法（`90:critical` ↔ `disable` 加 `_critical: "90"`）不再是變更（rc 1 → 0）；`70` → `70:critical` 是兩列（warning 被靜默、critical 新啟用，`--tenant` 亦然，可能 rc 0 → 1）；沒有告警比的 row（mongodb 的 critical row）照列不回測，mongodb `70` → `70:critical` 由 UNKNOWN 改為 MEDIUM。`--json` 每列帶 `keys`、`metric` 為告警 key（帶維度的列接上維度），不再帶 `old_severity`／`new_severity`。PromQL 放不進去的租戶 id 改列為不回測（先前 rc 2）。custom-alert recipe 的 NOTE 改列兩棵樹之間 recipe 有變的租戶（依 exporter 送出的 row；`_platform.yaml`、子目錄都算，只換檔不算）；`--config-dir` 有讀不了的檔改由 da-guard 指名（`reason` 由 `conf_file_unreadable` 改為 `served_values_unavailable`）。
