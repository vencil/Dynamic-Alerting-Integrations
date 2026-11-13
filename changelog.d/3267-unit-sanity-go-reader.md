---
section: Changed
topic: lint
issues: [3267]
created: 2026-11-01T10:34:19+00:00
---
- **⚠️ Breaking：unit-sanity 的每個閾值改由 exporter 讀（lint；[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：`check_threshold_unit_sanity.py` 不再用 Python 複本解析閾值，所有來源（conf.d、rule pack、registry、`docs/assets/*.json`、文件 YAML 範例）的原文一次送進新的 `da-guard threshold-values` 子命令（stdin 給 `[{key, text}]`，回 `number`／`non_finite`／`severity`／`refused`），`_lib_validation` 的解析複本已刪除。結束碼的改變：exporter 不當數字讀的寫法（`1_000`、`0x1p10`、數字超過 20 位，[#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)）一律不受檢；`+nan`／`-nan` 與非 ASCII 數字的溢位不再當成 NaN／inf 判，可能 1→0；被最後一筆 `+nan` 遮住的層級倒置現在會報出，0→1；沒有 da-guard 時，只有 docs JSON 的樹也是 2；不認得 `threshold-values` 的舊版 da-guard 為 2，請重建。
