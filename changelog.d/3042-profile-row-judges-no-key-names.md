---
section: Changed
topic: da-tools
issues: [3042]
created: 2026-10-21T15:48:21+00:00
---
- **profile 裡的 `_` 鍵改由 `validate-config` 的 `unread_keys` 列判，`profiles` 列不再判鍵名（[#3042](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3042)）**：`profiles` 列先前用自己的鍵名規則，把 exporter 在 profile 會讀的 `_custom_alerts`、`_silent_mode` 也報成「reserved keys belong in tenant config」，與保留鍵登錄表矛盾。現在由共用判決依登錄表的 profile 層判：沒有讀取端在 profile 讀的 `_` 鍵（登錄表沒有的，或像 `_profile` 這種不在 profile 讀的）在該 profile 檔上報一筆 notice，不論有沒有租戶綁定、值有沒有被租戶自己的值蓋掉。兩列都只報不擋，結束碼不變。範例樹 `rule-packs/recipes/examples/conf.d` 的 `finance` recipe 改寫在 profile。
