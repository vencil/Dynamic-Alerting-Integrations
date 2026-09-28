---
section: Fixed
topic: exporter
issues: [2291]
created: 2026-09-28T21:00:00+08:00
---
- **da-guard 的 routing 改讀 route generator 實際讀的來源，並點名寫錯位置的 routing（[#2291](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2291)）**：先前 routing 檢查與 `--required-fields _routing.*` 都讀合併後的有效設定，於是 `defaults:` 區塊、無包裝的 `_defaults.yaml` 頂層或 threshold profile 裡從不產生 route 的 `_routing` 被當成租戶 routing 判錯，而正確寫在根目錄 `_routing_defaults` 的 receiver 反被報 `missing_required`。現在租戶層只取租戶檔的 `_routing` / `_routing_profile`，蓋在根目錄平台檔 `tenants.<id>` 同名鍵之上。新 finding `routing_in_unread_location`（error）點名寫在上述位置的 `_routing` / `_routing_*` 並指引改寫處。平台檔用 YAML merge key（`<<:`）帶入的 `_routing` 照樣讀到。`_routing: disable` 的租戶在 `--required-fields _routing*` 下仍報 `missing_required`，但訊息改為註明是明示停用、與真的缺值分開。⚠️ 原本靠這些位置「通過」檢查的樹，會開始以 exit 1 被擋。
