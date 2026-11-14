---
section: Changed
topic: lint
issues: [3267]
created: 2026-11-01T10:34:19+00:00
---
- **⚠️ Breaking：unit-sanity 的每個閾值改由 exporter 讀（lint；[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：`check_threshold_unit_sanity.py` 不再用 Python 複本解析閾值，所有來源（conf.d、rule pack、registry、`docs/assets/*.json`、文件 YAML 範例）的原文一次送進新的 `da-guard threshold-values` 子命令（stdin 給 `[{key, text}]`，回 `number`／`non_finite`／`severity`／`refused`），`_lib_validation` 的解析複本已刪除。新 finding `NOT-A-NUMBER`：寫在閾值位置（key 的值、排程的 `default` 與各時段 `value`、registry 的 `value`、docs JSON 的值與百分位）而 exporter 讀不出數字、也不是 `disable` 的值——`1_000`、`0x1p10`、尾數超過 20 位（[#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)）、`abc`、`70:high`、`""`、`+nan`、非 ASCII 數字，以及 registry 與 docs JSON 該位置上的 `true`、`null`、陣列、物件。沒讀到數字不算檢查過。conf.d 的 YAML null（`key: ~`、`key:` 留空）不報：exporter 視為沒寫。結束碼的改變都只在帶 `--ci` 時（不帶 `--ci` 一律回 0，只列出發現）：上述值由略過改為 0→1；被最後一筆 `+nan` 遮住的層級倒置現在會報出，0→1。沒有 da-guard 時，只有 docs JSON 的樹也是 2；不認得 `threshold-values` 的舊版 da-guard 為 2，請重建。
