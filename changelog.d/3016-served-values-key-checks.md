---
section: Added
topic: da-guard
issues: [3016]
created: 2026-10-31T12:00:00+00:00
---
- **`da-guard served-values` 每個租戶新增 `key_checks`（ADR-038 決定 11）**：exporter 對租戶 key 的檢查，每筆一個記錄 `{kind, key, head, replacement_head, reason}`，依 key、kind 排序，沒有時為 `[]`。`kind` 是封閉集合：4 種 `version_label_*`（維度 key 的 `version` label 被 exporter 拒收）與 3 種 `renamed*`（舊拼法的 key，#1231；只有這類帶 `replacement_head`）；`reason` 是每個 kind 一句固定說明，只有 `key`、`head` 是作者寫的字。Python 讀取端照記錄印、不自己判。文件 schema 隨之升版（見 #3703 條目）。
