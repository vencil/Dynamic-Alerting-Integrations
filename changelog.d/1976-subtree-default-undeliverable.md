---
section: Added
topic: confd-family
issues: [1976]
created: 2026-10-03T05:45:35+00:00
---
- **da-guard 新增 `subtree_default_undeliverable` 警告，`served-values` 的 `unserved` 列出同一批 key（[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）**：子目錄 `_defaults.yaml` 寫了根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒宣告的 key 時，`/effective` 顯示該值，exporter 卻送不出去（載入時印 ERROR、`da_config_subtree_undeliverable_tenants` 計入）；先前 da-guard 不報、`served-values` 也不列。現在 da-guard 對 `--scope` 內每個受影響的租戶與 key 各報一條 warn（`--warn-as-error` 時結束碼 1），判定直接取 exporter 自己載入的結果。修法：把 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`。⚠️ 目前為 warn，預計下一個 minor 版改為 error（不加 `--warn-as-error` 也會擋），請先修好。exporter 的 ERROR 與 gauge 不變。
