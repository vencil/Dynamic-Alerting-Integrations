---
section: Changed
topic: exporter
issues: [3556]
created: 2026-11-05T06:30:00+00:00
---
- **#1231 新舊拼法只由 exporter 判定，`<舊名>_critical{…}` 也算同一個閾值（[#3556](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3556)）**：別名套在鍵的前段（`{` 之前），完全相同或帶 `_critical`，`{…}` 原樣保留。新增 `da-guard key-spellings`（stdin 鍵陣列 → 每個鍵的現行拼法與所有拼法）；Policy-as-Code、`diagnose`、`backtest-threshold`、`threshold-recommend` 改問它，Python 不再自己換名，`served-values` 也不再輸出別名表。對 `<舊名>_critical{…}` 的影響：同一層也寫了新拼法時舊拼法判 `shadowed`（error）；跨層時照同一個閾值逐層覆蓋，較高層勝出；計入 `da_config_deprecated_keys`；`key-refs` 把 `<base>_critical{…}` 歸在 `<base>`；以舊拼法寫的萬用字元規則也比得到維度鍵。這些工具需要帶 `key-spellings` 的 da-guard，太舊的回結束碼 2。
