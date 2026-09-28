---
section: Fixed
topic: confd-reader-consistency
issues: [2297]
created: 2026-09-28T17:37:21+00:00
---
- **`describe_tenant.py`、`validate-config`、`config-diff`、`diagnose` 以原始文字讀租戶的 `_profile`，與 exporter 一致（tools；[#2297](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2297)）**：`_profile: 010` 沒加引號時，exporter 套用 profile `010`，這四支工具卻以 YAML 1.1 型別讀成 `8`。結果是 `describe_tenant.py` 回報的生效值沒有套到 profile；`validate-config` 的 profiles 檢查把整數略過，指向不存在 profile 的 `_profile: 123` 也回 PASS；`config-diff` 的 profile 影響清單漏掉該租戶，`010` 改成 `8` 也報「沒有變更」；`diagnose` 找不到 profile，繼承鏈缺 profile 那一層。現在四者都以原文比對，`_profiles.yaml` 的 profile 名稱也以原文讀（`010:` 就是 `010`）。未加引號的 `123` 若沒有對應的 profile，`validate-config` 會回報 unknown profile。
