---
section: Fixed
topic: confd-family
issues: [2707]
created: 2026-10-03T23:47:55+00:00
---
- **da-guard 與子目錄 defaults 的判定不再把 `optional_overrides:` 裡 `_` 開頭的閾值 key（保留鍵以外）當成已宣告（[#2707](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2707)）**：`optional_overrides:` 本來就不送這類 key，列在那裡卻讓判定沉默。這類 key 在任何一層都不產生閾值列：子目錄 `_defaults.yaml` 給的值由 `subtree_default_reserved_key` 報，不計入 exporter 的 ERROR 與 `da_config_subtree_undeliverable_tenants`；根層寫 null 時也不由 `root_default_null_undeclared` 指名。`served-values` 的 `values` 不變，`unserved` 不再列出子目錄給的 `disable` 等關掉的值與 `_silent_*` 形狀的值。保留鍵照舊。
