---
section: Fixed
topic: alertmanager-routing
issues: [2730, 2486]
created: 2026-10-08T15:34:20+00:00
---
- **exporter 與 tenant-api 讀 YAML 時保留非特定 tag `!`（exporter；[#2730](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2730)）**：vendored yaml.v3 加一段 patch，寫了非特定 tag `!` 的 quoted／block scalar 保留這個 tag。當 key 時與產生器（PyYAML）一致：`! "<<"` 是 merge key（`! "<<": *base` 併入 `base` 的鍵，原本會留下一個名為 `<<` 的鍵）。解碼成值時仍是字串（`! "true"` 是 `"true"`；PyYAML 讀成布林 True，這點兩邊不同）。寫回 YAML 時 `!` 原樣保留，不再悄悄變成 `"true"`。
