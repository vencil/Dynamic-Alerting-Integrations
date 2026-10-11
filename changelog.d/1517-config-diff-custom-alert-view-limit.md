---
section: Changed
topic: da-tools
issues: [1517]
created: 2026-10-10T14:30:00+08:00
---
- **`config-diff` 的 Custom Alert 區段寫明它不判定消音（[#1517](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1517)）**：刪掉一條會 page 的 recipe 時，config-diff 只列 removed，沒有任何說明它判不判定消音。現在區段開頭的警語下多一段說明：這一段讀的是 exporter 送出的 row，`threshold` 是 `<值>:<severity>`，`op`／`window`／`for` 的變更以 `recipe_id` 不同呈現；不再送出的 recipe 列為 removed，並註明分不出是刪除、停用還是被 exporter 拒收。只有原地改成停用的 threshold 或 `mode: silent` 會另外標示。
