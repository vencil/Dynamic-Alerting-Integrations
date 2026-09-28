---
section: Fixed
topic: confd-reader-consistency
issues: [2216, 2237]
created: 2026-09-28T16:04:16+08:00
---
- **`patch-config` 寫回時以原始文字讀租戶 id，與 exporter 一致（tools；[#2237](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2237)、[#2216](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2216)）**：寫入前重讀設定時，過去以 YAML 1.1 型別讀 `tenants:` 的 key。因此不加引號的 `8`、`010`、`yes` 這類租戶一律無法 patch（rc 2）；同一份 `config.yaml` 裡的 `8` 與 `010` 會被合成一個，patch 其中一個會連另一個一起覆寫（寫後驗收會回滾，但回滾前 exporter 服務的是錯的值）。現在只會改到指名的租戶，id 寫回後不變；租戶的 `_profile: 010` 也不再被寫成 `_profile: 8`。其他值被改寫型別的問題另於 [#2220](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2220) 處理。
