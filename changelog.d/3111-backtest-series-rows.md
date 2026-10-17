---
section: Changed
topic: threshold-governance
issues: [3111]
created: 2026-10-17T10:55:19+00:00
---
- **`backtest` 以 `/metrics` series 為單位回報，`--git-diff` 改比兩棵樹（[#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)）**：⚠️ 行為與結束碼變更。`--git-diff` 不再解析 diff 文字：由 git 自己把 `HEAD~1` checkout 到暫存目錄（不動 repo），與工作樹的 `conf.d/` 一起交給 exporter 讀（需要 da-guard）；檔名、子目錄、改名、symlink、換行轉換照 checkout，租戶取自 `tenants:`；submodule、解析後落在 checkout 外的 link exit 2。三種模式同一規則，每列是一條 series：只換寫法（`90:critical` ↔ `disable` 加 `_critical: "90"`）不再是變更（rc 1 → 0）；`70` → `70:critical` 是兩列（warning 被靜默、critical 新啟用，`--tenant` 亦然，可能 rc 0 → 1）；mongodb 的 critical row 沒有告警比，照列不回測（`70` → `70:critical` 由 UNKNOWN 改為 MEDIUM）。`--json` 每列帶 `keys`、`metric` 為告警 key（帶維度的列接上維度），不再帶 `old_severity`／`new_severity`。PromQL 放不進去的租戶 id 改列為不回測（先前 rc 2）。
