---
section: Changed
topic: dx
issues: [2059]
created: 2026-09-27T03:48:47+00:00
---
- **`lint-policy.md` 刪掉沒有機制維護的數量與清單（docs；[#2059](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2059)）**：標題、導言與 §2 的 lint 數量、commit scope 數量刪掉；§6 的各類別逐項清單、括號計數與 (b) 類對照表刪掉，改成指向各 lint 自己 docstring 的 `Lint class` 段；§8 的 JS-toolchain grandfather 對照表刪掉，改指向 `check_lint_toolchain_fit.py` 的 `ALLOWLIST`。§2 的 (a) 類範例拿掉 docstring 自述為 (b) 類的 `check_changelog_no_tbd.py`。
