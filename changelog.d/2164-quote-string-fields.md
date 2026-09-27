---
section: Changed
topic: confd-family
issues: [2164]
created: 2026-09-27T17:21:25+00:00
---
- **conf.d 字串型欄位的值未加引號、PyYAML 卻讀成非字串時，從「通過」改為「失敗」；`_routing_enforced.enabled` 必須是布林（tools；[#2164](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2164)）**：`channel: yes` 在 PyYAML 是 `True`，在 exporter 與 Alertmanager 是字串 `"yes"`，三方讀到不同的值。`check_confd_schema` 與 `validate-config`（新增 `yaml_quoting` 列）現在對 schema 標為字串（含 enum）的欄位，只要值未加引號且被讀成 bool／數字／null，就回報檔案、行號與欄位路徑並 exit 1；解決方式是加引號。租戶的閾值也是字串欄位，未加引號的數字（`mysql_connections: 70`）同樣會被擋。`platform-defaults.schema.json` 的 `_routing_defaults`／`_routing_enforced` 改為引用租戶 schema 的定義，不再接受任何值。`_routing_enforced.enabled` 寫成 `n`、`'yes'` 等非布林值時，過去會啟用 NOC 強制路由；現在不啟用，且 `generate-routes --validate` 與 `validate-config` 回 1。
