---
section: Changed
topic: threshold-governance
issues: [3111]
created: 2026-10-17T10:55:19+00:00
---
- **`backtest --git-diff` 改比兩棵樹，變更以 `/metrics` series 為單位（[#3111](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3111)）**：⚠️ 行為與結束碼變更。`--git-diff` 不再解析 `git diff` 文字，改把 `HEAD~1` 的 `conf.d/` 取出成一棵樹、與工作樹的一起交給 exporter 讀（同 `--config-dir`，因此需要 da-guard）：任何檔名、子目錄、改名都照 exporter 讀，租戶取自 `tenants:`，先前被丟掉的檔名照常回報；`HEAD~1` 有 exporter 讀不了的檔時 exit 2。每一列是一條 series：只換寫法（`90:critical` ↔ `disable` 加 `_critical: "90"`）不再是變更（rc 1 → 0）；`70` → `70:critical` 是兩列（warning 被靜默、critical 新啟用，可能 rc 0 → 1）；`--json` 每列帶 `keys`，`metric` 改為比較它的告警 key，不再帶 `old_severity`／`new_severity`。`--tenant` 沒有出廠告警比較的那一邊視同沒有門檻：mongodb 的 `70` → `70:critical` 由 UNKNOWN 改為 MEDIUM，反向可能 HIGH。
