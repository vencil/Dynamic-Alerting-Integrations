---
section: Changed
topic: exporter
issues: [3140]
created: 2026-10-21T18:30:00+00:00
---
- **backtest 與 config-diff 讀出「根 defaults 非有限而沒有預設列」（[#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)）**：根 `_defaults.yaml` 的 `defaults:` 把租戶沒寫的 key 改成 `.nan`／`.inf`／`-.inf` 時，`backtest --config-dir` 依 served-values 的 `keys[].reason` `root_default_non_finite` 判 HIGH（先前判「Metric disabled」MEDIUM、結束碼 0；`.nan`／`.inf` 恢復為 #3140 前的 1），config-diff 在該列標「(not served)」，不再看起來像單純移除。⚠️ `-.inf` 在 #3140 前依歷史資料計算，可能是 0；現在一律 1。
