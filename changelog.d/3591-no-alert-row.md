---
section: Added
topic: tools
issues: [3591]
created: 2026-11-07T11:00:00+08:00
---
- **validate-config 新增 `no_alert` 列：送出了但沒有告警比較的閾值鍵（[#3591](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3591)）**：例如根 `defaults:` 的錯字、規則包沒有 critical 層時的 `<key>_critical`。以 `metric_observed_map.yaml` 判斷，每個鍵一行、寫出值來自的檔與租戶；永遠只是建議（WARN、exit 0）。只認出貨的規則包，客戶自己規則讀的鍵也會列出。範例 conf.d 目前報 mongodb、redis 的 3 個 `_critical` 鍵（規則包缺 critical 層，[#3658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3658)）。
