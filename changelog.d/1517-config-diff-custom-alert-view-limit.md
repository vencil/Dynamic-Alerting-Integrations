---
section: Changed
topic: da-tools
issues: [1517]
created: 2026-10-10T14:30:00+08:00
---
- **`config-diff` 的 Custom Alert 區段寫明它不判定消音（[#1517](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1517)）**：刪掉一條會 page 的 recipe 時，config-diff 只列 removed，沒有任何說明它判不判定消音。現在區段開頭的警語下多一段：這裡只比對頂層租戶檔自己的 recipe、不解析 `_defaults.yaml` 繼承，只有原地改成停用的 threshold 或 `mode: silent` 會標示，刪除的 recipe 是否停掉 page、以及繼承來的 recipe，都不在這份報告的判定範圍內。
