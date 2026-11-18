---
section: Changed
topic: rule-packs
issues: [1176]
created: 2026-11-18T16:30:00+00:00
---
- **DB2 `DB2HighSortOverflow` 加最小樣本門檻：5 分鐘內排序不到 200 次就不計算溢位比例（[#1176](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1176)）**：之前只要求分母大於 0。閒置庫 5 分鐘 3 次排序、1 次溢位，比例就是 33%，持續 10 分鐘會發出告警，但這個比例根本不能拿來和 0.05 門檻比。200 次是比例 success-failure 條件在 0.05 門檻下的樣本數（n·p₀ ≥ 10），只由門檻決定，與流量無關。它只保證比例值有意義，不能壓低誤報：門檻附近的雜訊仍靠 `for: 10m` 吸收。代價是排序量低於這個值的庫，即使溢位很嚴重也不會告警。0.05 門檻本身仍待真實流量校準。
