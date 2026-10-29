---
section: Added
topic: confd-family
issues: [1976, 3378, 3379]
created: 2026-10-03T05:45:35+00:00
---
- **da-guard 新增 `subtree_default_undeliverable` error，`served-values` 的 `unserved` 列出同一批 key（[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）**：子目錄 `_defaults.yaml` 寫了根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒宣告的閾值 key 時，`/effective` 顯示它，exporter 卻送不出去；子目錄的 `<base>_critical` 在根目錄 `defaults:` 沒有基底值、也沒有該 key 本身的值時同屬此類（[#3378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3378)）。先前 da-guard 不報、`served-values` 也不列。現在 da-guard 對 `--scope` 內每個受影響的租戶與 key 各報一條 error，結束碼 1；exporter 載入時印 ERROR 並計入 `da_config_subtree_undeliverable_tenants`，與 da-guard 讀同一份判定（[#3379](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3379)）。修法：把該 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`（`_critical` 則給基底一個根目錄值）。保留鍵與 `_silent_*`、`_state_*` 等鍵改由 `subtree_default_reserved_key` 報（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)），`_routing_*` 交 route generator；子目錄在每個時段都關掉、根目錄沒宣告的 key 兩邊都不報，exporter 改印一行 WARN。
