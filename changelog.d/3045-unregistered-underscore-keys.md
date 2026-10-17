---
section: Changed
topic: exporter
issues: [3045]
created: 2026-10-17T02:05:40+00:00
---
- **BREAKING — exporter 不再送出保留鍵登錄表沒有的 `_` 開頭鍵（[#3045](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3045)）**：過去根目錄 `_defaults.yaml` 的 `defaults:` 宣告了 `_myth` 這類鍵，exporter 就把它（含 `_myth_critical`、`_myth{…}`）當成閾值送出，與 JSON schema 拒收的判斷相反。現在不論寫在哪一層都不產生列，依賴這種寫法的部署升級後會少掉那些 `user_threshold` 列。served-values 把它列進 `unserved`（reason `unregistered`，根目錄的也列給每個租戶），`da-guard effective` 的 `not_served` 標 `unregistered`；子目錄的這種鍵改報 `subtree_default_reserved_key`。**遷移**：租戶這一側的這種鍵 `validate-config` 的 schema 檢查已會擋下；根目錄 `defaults:` 裡的 schema 看不到，用新版 `da-guard served-values` 找 `keys` 裡 reason 為 `unregistered` 的鍵。改成登錄表有的鍵，或拿掉 `_` 前綴讓它成為一般閾值鍵（根 defaults 與租戶檔一起改）；`config-diff` 會列出改名後出現的閾值列。
