---
section: Changed
topic: exporter
issues: [3016]
created: 2026-10-31T13:00:00+00:00
---
- **⚠️ breaking：維度鍵要平台宣告了 base 才送出（ADR-038 決定 11）**：租戶的 `<base>{label="v"}` 只有在 base 列在根 `_defaults.yaml` 的 `defaults:`（有值）或 `optional_overrides:`，或根 `defaults:` 寫了整個鍵時，exporter 才送出那一列，與一般鍵同一個成員條件。base 拼錯或沒宣告的維度鍵不再送出（先前照送，但沒有任何告警讀），共用判決報 `unread … (no_row)`，tenant-api 寫入擋下，exporter 的 `config keys no row of this exporter reads` 行列為 `reason=unconsumed`。依賴這種鍵的部署（例如自訂規則讀它）要在 `optional_overrides:` 宣告該 base。出貨的 conf.d 範例（es／redis／mongo）與 rule pack 檔頭拿掉維度鍵：出貨的 rule pack 以 `max by(tenant)` 取租戶最大值，維度鍵只會改到整個租戶的門檻（各維度值與 base 合成一個值）、寫 `disable` 等於沒寫、不會只停掉那個維度，要依維度判斷得改 rule pack（[#3475](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3475)）；語法文件保留並註明這點。
