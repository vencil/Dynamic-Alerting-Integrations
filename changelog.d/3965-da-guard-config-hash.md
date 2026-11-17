---
section: Added
topic: exporter
issues: [3965]
created: 2026-11-17T19:30:00+00:00
---
- **da-guard 每份走過 `--config-dir` 的 JSON 文件都帶 `config_hash`（da-tools；[#3965](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3965)）**：預設檢查、`parse-tree` tree mode、`served-values`、`effective`、`key-verdict`、`key-refs`、`compile-custom-alerts`、`severity-dedup` 都回報這次 walk 讀到的樹的身分，與 exporter `/api/v1/config/identity` 的 `config_hash` 是同一個定義；exit 3 時照帶，`effective --overlay` 為 `null`。讀取端可據此判斷兩次 walk 是否讀到同一棵樹（判定後樹變了卻照常回答的缺口，由後續的比對處理）。測試把 dispatch table 當成母體：新增的子命令沒有歸類就轉紅。
