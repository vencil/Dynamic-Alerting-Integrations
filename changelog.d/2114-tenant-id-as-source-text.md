---
section: Fixed
topic: confd-reader-consistency
issues: [2114]
created: 2026-09-27T16:50:00+08:00
---
- **租戶 id 一律以原始文字讀取，與 exporter 一致（tools；[#2114](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2114)）**：Python 工具過去以 YAML 1.1 型別讀 `tenants:` 的 key，`010` 變成 `8`、`0x1F` 變成 `31`、`yes` 變成 `True`；exporter 則以 key 的原始文字為租戶 id。現在所有讀租戶的 Python 工具都讀原始文字：輸出顯示 `"010"` 而不是 `8`，JSON 輸出的租戶 id 一律是字串；兩個檔裡的 `123` 與 `"123"` 在 Python 端也算同一個租戶（exporter 本來就是），平台檔 `tenants:` 區塊因此能對上租戶檔。`generate-routes` 遇到 `yes`、`010` 這類不加引號的租戶 id 不再以 `TypeError` 中止，並產出 `tenant="010"` 這類 matcher。policy 的 `exclude_tenants` 與 domain policy 的 `tenants` 清單也改以文字比對，`exclude_tenants: [123]` 能排除寫成 `"123"` 的租戶。
