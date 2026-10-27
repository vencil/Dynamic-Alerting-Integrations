---
section: Added
topic: confd-family
issues: [3206]
created: 2026-10-27T17:46:25+00:00
---
- **`da-guard write` 的刪除項目可帶 `tenants` 前提（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §2）**：`{"path", "delete": true, "tenants": [<id>, ...]}` 只在這次判定讀到的那份檔是租戶檔、`tenants:` 恰好宣告這些 id 時才刪；否則整批拒絕（新的拒絕原因 `declares_mismatch`，訊息點名實際宣告了哪些），什麼都不寫。判定之後檔被改過照舊是 `changed_during_write`。⚠️ 這是比對、不是鎖：最後一次比對到 unlink 之間對同一個檔的就地改寫看不到。沒給 `tenants` 時行為不變。`_lib_confedit.delete_entry(path, tenants=...)` 送這個欄位。
