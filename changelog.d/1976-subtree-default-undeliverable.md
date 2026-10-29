---
section: Added
topic: confd-family
issues: [1976, 3378]
created: 2026-10-03T05:45:35+00:00
---
- **da-guard 新增 `subtree_default_undeliverable` error，`served-values` 的 `unserved` 列出同一批 key（[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）**：子目錄 `_defaults.yaml` 寫了根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒宣告的閾值 key 時，`/effective` 顯示它，exporter 卻送不出去（載入時印 ERROR、`da_config_subtree_undeliverable_tenants` 計入）；子目錄寫的 `<base>_critical`，基底須在根目錄 `defaults:` 有值（根目錄寫成 null、只在 `optional_overrides:` 宣告都不算），或根目錄直接給了該 `_critical` key 的值，否則也是這一類（[#3378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3378)）。先前 da-guard 不報、`served-values` 也不列。現在 da-guard 對 `--scope` 內每個受影響的租戶與 key 各報一條 error，結束碼 1，判定直接取 exporter 自己載入的結果；`unserved` 中的值是最深一層寫成閾值形狀的值、經正規化。修法：把該 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`（`_critical` 則給基底一個根目錄值）。保留鍵與 `_silent_*`、`_state_*` 等鍵改由 `subtree_default_reserved_key` 報（[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)），關掉的 key 不報。
