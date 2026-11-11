---
section: Changed
topic: tenant-api
issues: [3683]
created: 2026-11-11T03:15:00+00:00
---
- **寫入關卡擋不合法的 window，排程條目各只報一次（tenant-api、exporter；[#3683](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3683)）**：`window:` 不是 UTC `HH:MM-HH:MM` 或起訖相同的條目，exporter 一分鐘都不送；tenant-api 以前只在值也讀不了時才擋。現在新寫或改過的不合法 window 一律回 400（batch 該筆 op 422），檔裡原封不動（條目位置、window、值都相同）的只回 NOTICE，與死條目同一種 ratchet。死條目與不合法 window 裡的值不再另報「exporter 讀不了的閾值」，訊息只剩讓它送不出的那個缺陷；刪掉這種條目也不再讓同一個 key 上原有的讀不了的值被當成改動（非有限數照 #3140 一律擋）。⚠️ 破壞性變更：值合法、只有 window 不合法的寫入以前回 200。
