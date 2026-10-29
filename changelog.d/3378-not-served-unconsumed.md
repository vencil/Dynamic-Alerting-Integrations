---
section: Changed
topic: exporter
issues: [3378]
created: 2026-10-29T00:30:00+00:00
---
- **`not_served` 新增 `unconsumed`；子目錄的 `_critical` 只在基底有根目錄值時交付（threshold-exporter / da-guard；[#3378](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3378)）**：租戶檔、平台檔 `tenants:` 條目或 profile 寫了根目錄 `defaults:` 與 `optional_overrides:` 都沒宣告的閾值 key（含拼錯的 key），或基底在根目錄 `defaults:` 沒有值的 `_critical`，`/metrics` 不送，先前只有載入時一行 `unknown key`；現在 `da-guard effective` 的 `not_served` 標 `unconsumed`（resolver 自己的記錄；每個時段都 `disable` 的、維度 key 不標，根目錄寫成 null 的仍標 `root_null_undeclared`）。它不是 `value_not_served` 的原因，等級由 ADR-038 的共用判決決定。⚠️ 行為變更：子目錄 `_defaults.yaml` 寫的 `<base>_critical`，基底只在 `optional_overrides:` 宣告或根目錄沒有時，改報 `subtree_default_undeliverable`（error，exit 1）並計入 `da_config_subtree_undeliverable_tenants`；先前被寫進租戶設定、不送也不報。
