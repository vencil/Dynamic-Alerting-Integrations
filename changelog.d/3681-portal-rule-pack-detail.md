---
section: Changed
topic: portal
issues: [3681]
created: 2026-11-10T15:31:43+00:00
---
- **Portal「Rule Pack 詳情」不再手抄規則，改顯示生成的 pack 資料並連到告警參考與 Rule Pack YAML（[#3681](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3681)）**：原本手抄的 26 條 recording 與 45 條 alert 一直在漂移（缺條目、expr 少了 maintenance 排除、計數錯誤、4 個 pack 整個沒列），守它的 `check_portal_rulepack_claims.py` 只比對名稱，看不到這些。頁面現在列出全部 16 個 pack 的類別、exporter 與規則數（取自 platform-data），規則本身連到已生成、有 drift gate 的 `ALERT-REFERENCE` 與 pack YAML；該守衛隨之退役。⚠️ 頁面不再逐條列出 record／alert 與 PromQL。
