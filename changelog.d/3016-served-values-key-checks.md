---
section: Added
topic: da-guard
issues: [3016]
created: 2026-10-31T12:00:00+00:00
---
- **`da-guard served-values` 每個租戶新增 `key_checks`（ADR-038 決定 11）**：exporter 對租戶 key 的兩項檢查，照它自己的措辭：`version_labels`（維度 key 的 `version` label 不合 ADR-024 OQ-6）與 `renames`（舊拼法的 key，#1231），各為排序過的字串清單，沒有時為 `[]`。Python 讀取端照印、不自己判。不改 schema 版號：讀不到這個欄位的讀取端把它當成較舊的 da-guard。
