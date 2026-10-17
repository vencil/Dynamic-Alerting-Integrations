---
section: Changed
topic: exporter
issues: [3045]
created: 2026-10-17T02:05:40+00:00
---
- **BREAKING — `_` 開頭的鍵不再成為閾值；登錄表沒有的 `_` 鍵在任何一層都不讀（[#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)）**：過去根目錄 `_defaults.yaml` 的 `defaults:` 寫了 `_myth`（登錄表沒有）或 `_namespaces: 5`（登錄表有、但不是閾值）這類鍵，exporter 就把它（含 `_critical`、`{…}` 形狀）送成 `user_threshold` 列，與 JSON schema 相反。現在 `_` 鍵在任何一層都不產生閾值列，`expires:` 也不為它發出過期事件；依賴這種寫法的部署升級後少掉那些列。登錄表沒有的 `_` 鍵：served-values 列進 `unserved`（reason `unregistered`，根目錄的也列給每個租戶），`da-guard effective` 的 `not_served` 標 `unregistered`，tenant-api 寫入檢查即使根目錄宣告了也報「unknown reserved key」並拒收；子目錄的改報 `subtree_default_reserved_key`。**遷移**：改用登錄表有的鍵，或拿掉 `_` 前綴讓它成為一般閾值鍵（根 defaults 與租戶檔一起改）；根目錄 `defaults:` 裡的這類鍵 schema 看不到，用 `validate-config` 的 `unread_keys` 列或新版 `da-guard served-values` 找。
