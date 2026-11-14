---
section: Changed
topic: tools
issues: [3829]
created: 2026-11-13T10:01:54+00:00
---
- **退役的閾值 key 一律報錯，並點名替代 key（validate-config／da-guard／tenant-api；[#3829](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3829)）**：threshold registry 新增 `retired_keys:`，登記已沒有告警規則讀的舊閾值 key 與它的替代 key（它的 `_critical` 一併退役）。這類 key 寫在任何一層——包含你自己的根 `_defaults.yaml`（`da-tools init` 產生的檔就會有）——都判為 `unread`、原因 `retired`、等級 error：validate-config 的 `routes` 列 FAIL（rc 1）並印出該寫哪個 key，`da-guard write` 與 tenant-api 寫入也會擋，訊息同樣點名替代 key。之前根 `defaults:` 寫了舊 key 只出 `no_alert` 建議、rc 0。tenant-api 的 batch `unset` 可以直接刪掉退役 key。⚠️ 升級後，樹裡還留著退役 key 的部署會轉紅，直到改成替代 key 或刪掉為止；本版登記的是 [#3783](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3783) 移除的 Kafka key，之後的 rule pack 改名會各自登記。
