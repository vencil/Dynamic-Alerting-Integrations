---
section: Fixed
topic: portal
issues: [3957]
created: 2026-11-15T05:34:39+00:00
---
- **Routing Trace 照 exporter 的規則讀閾值（portal；[#3957](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3957)）**：這一頁原本用 `parseFloat` 讀閾值，`0x1F` 讀成 31、`"70:high"` 讀成 70、`1_000` 讀成 1，`_critical: "80:critical"` 也被當成有 critical tier，於是 warning 被畫成「被 inhibit」。現在改用與 Alert Preview 相同的讀法：exporter 不收的值不當成數字，並顯示同一句說明；`_critical` 整個值當數字讀，`80:critical` 不算 critical tier。`_routing` 的讀法不變。
