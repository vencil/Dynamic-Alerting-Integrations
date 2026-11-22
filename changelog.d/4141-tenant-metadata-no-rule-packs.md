---
section: Removed
topic: confd-family
issues: [4141]
created: 2026-11-21T09:39:41+00:00
---
- **⚠️ Breaking：租戶元資料不再有 `rule_packs`，portal 租戶卡片拿掉規則包標籤（[#4141](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4141)）**：`generate_tenant_metadata.py` 與 `platform-data.json` 內嵌的 `tenant_metadata` 先前依 `/metrics` 送出的閾值鍵名推斷規則包；讀取端內建平台預設啟用的閾值之後（[#3832](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3832)），每個租戶都送這些鍵，推出來的規則包全部相同，沒有資訊量。租戶宣告的是 `_expected_exporters`，卡片上有「Expected exporters」這一列。讀 `tenant_metadata.<租戶>.rule_packs` 的程式請改讀 `expected_exporters`。
