---
section: Added
topic: confd-family
issues: [1976]
created: 2026-10-03T05:45:35+00:00
---
- **da-guard 新增 `subtree_default_undeliverable` 警告，`served-values` 的 `unserved` 列出同一批 key（[#1976](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1976)）**：子目錄 `_defaults.yaml` 寫了根目錄 `_defaults.yaml` 與 `optional_overrides:` 都沒宣告的閾值 key 時，`/effective` 顯示它，exporter 卻送不出去（載入時印 ERROR、`da_config_subtree_undeliverable_tenants` 計入）；先前 da-guard 不報、`served-values` 也不列。現在 da-guard 對 `--scope` 內每個受影響的租戶與 key 各報一條 warn（`--warn-as-error` 時結束碼 1），判定直接取 exporter 自己載入的結果；`unserved` 中這類 key 的值是最深一層寫成閾值形狀的值、經正規化，不是原文。修法：把該 key 宣告在根目錄 `_defaults.yaml` 或 `optional_overrides:`；`_` 開頭的 key 只能宣告在根目錄（`optional_overrides:` 不服務它們，宣告在那裡值不送出，警告、ERROR 與 gauge 也照樣在）。保留鍵與 exporter 本來就不產生閾值列的 `_silent_*`、`_state_*` 等鍵不以此 finding 報（改由 `subtree_default_reserved_key` 報，見 [#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)），關掉的 key 也不報。⚠️ 目前為 warn，預計下一個 minor 版改為 error，請先修好。exporter 的 ERROR 與 gauge 不變。
