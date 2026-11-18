---
section: Changed
topic: rule-packs
issues: [1176]
created: 2026-11-18T16:30:00+00:00
---
- **DB2 `DB2HighSortOverflow` 加上最小樣本門檻：5 分鐘內排序不到 200 次就不計算溢位比例（[#1176](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1176)）**：之前只要求分母大於 0，閒置庫 5 分鐘內 3 次排序、1 次溢位就算出 33%。這個比例遠超 0.05 門檻，持續 10 分鐘就會發出告警，但這只是取樣雜訊，不代表 SORTHEAP 不足。200 次是比例檢定的 success-failure 條件在 0.05 門檻下推出的樣本數（n·p₀ ≥ 10），只和門檻有關，不依各租戶的流量而定。排序量低於這個值的租戶沒有比例，`tenant:db2_sort_overflow_ratio:avg` 也不會產出 series。0.05 門檻本身是否合適，仍要用真實流量校準。
