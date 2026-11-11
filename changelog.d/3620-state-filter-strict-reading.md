---
section: Changed
topic: exporter
issues: [3620]
created: 2026-11-06T12:00:00+08:00
---
- **⚠️ BREAKING：`_state_<filter>` 只讀得懂的值才開啟，讀不懂的不再當成開啟（[#3620](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3620)）**：純量只認 `enable` 與 `disable`（及別名）；物件只限 `_state_maintenance`，開關是 `target`／`enabled`。讀不懂的值不開啟、印 WARN，tenant-api 拒收（檔裡原本就有、沒改動的只回 NOTICE）。升級時翻轉的寫法：`"yes"`／`"true"`／`"1"`／空字串／亂打的字 → 關；`{target: disable}`、`{enabled: false}` → 關；`target` 不是 enable／停用字（如 `all`）、有未知欄位、`recurring` 條目不是物件 → 整個值讀不懂、關；只有 `recurring` → 不再一直維護，只在週期窗內開啟（見 [#3639](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3639)）；`{enabled: false, recurring: …}` → 窗內也不開；`expires` 空白或不是 RFC3339 → 整個值不讀、關（空白的 `expires` 不再當成永久維護）。
