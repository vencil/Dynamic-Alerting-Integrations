---
section: Changed
topic: exporter
issues: [3016]
created: 2026-10-31T13:00:00+00:00
---
- **⚠️ breaking：維度鍵要平台宣告了 base 才送出（ADR-038 決定 11）**：租戶的 `<base>{label="v"}` 只有在 base 列在根 `_defaults.yaml` 的 `defaults:`（有值）或 `optional_overrides:`，或根 `defaults:` 寫了整個鍵時，exporter 才送出那一列，與一般鍵同一個成員條件。base 拼錯或沒宣告的維度鍵不再送出（先前照送，但沒有任何告警讀），共用判決報 `unread … (no_row)`，tenant-api 寫入擋下，exporter 的 `config keys no row of this exporter reads` 行列為 `reason=unconsumed`。依賴這種鍵的部署（例如自訂規則讀它）要在 `optional_overrides:` 宣告該 base。出貨的範例與 scaffold-tenant 改用已宣告的 base。
