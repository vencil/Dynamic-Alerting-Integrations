---
section: Changed
topic: exporter
issues: [3556]
created: 2026-11-05T06:30:00+00:00
---
- **⚠️ breaking：#1231 新舊拼法的換名規則只留 Go 一份（[#3556](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3556)）**：別名套在鍵的前段，完全相同或帶 `_critical`，`{…}` 原樣保留；`<舊名>_critical{…}` 也換成新名。新增 `da-guard key-spellings`（stdin 鍵陣列 → 每個鍵的現行拼法與所有拼法），Policy-as-Code、`diagnose`、`backtest-threshold`、`threshold-recommend` 改問它，Python 不再自己換名：刪除 `_lib_tenant_values.canonical_key`、`PolicyInputs.aliases`、`evaluate_policies` 的 `aliases` 參數（`analyze_tenant` 改收 `spellings`）、`_grar_validate` 的別名表副本與換名函式，共用判決刪掉 `policyCanonical`。行為變更：同一層同時寫 `<舊名>_critical{…}` 與新拼法時，舊拼法判 `shadowed`（error）並出現改名 NOTICE；`key-refs` 把 `<base>_critical{…}` 歸在 `<base>`；以舊拼法寫的萬用字元規則也比得到維度鍵。這些工具需要帶 `key-spellings` 的 da-guard，太舊的回結束碼 2。
