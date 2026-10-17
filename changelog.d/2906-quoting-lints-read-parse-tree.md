---
section: Changed
topic: alertmanager-routing
issues: [2906]
created: 2026-10-17T12:00:00+00:00
---
- **⚠️ Breaking：`validate-config` 的每一列與 `check_confd_schema.py` 改讀 `da-guard parse-tree`，引號檢查以 yaml.v3 讀到的值為準（validate-config／tools；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：一次 `validate-config` 只有一種讀法——`json_schema`、`yaml_quoting`、`custom_alert_sources`（編譯器在這一列改用傳入的讀法；`compile_custom_alerts` 本身不變）、`unread_keys`、`root_defaults` 先前仍用 PyYAML。`yaml_quoting` 只列 yaml.v3 讀成非字串的值：`channel: yes` 不再列出（那是字串 `"yes"`），`channel: 1e3`、`name: 2024-01-02` 新列出；finding 只有檔案與欄位路徑，沒有行號。布林欄位寫 `yes`／`no`／`on`／`off` 時，加不加引號都是同一則 WARN，`json_schema` 同時 FAIL。`!!bool yes`、`!!int x` 這類 yaml.v3 套不上的 tag 讓整份檔讀不了：`check_confd_schema.py` 結束碼 2 並帶 `kind`，`validate-config` 由 `yaml_syntax` 報出；#2740 依 parse_failed 保留 WARN 的路徑移除。多份 document 的檔同樣讀不了：`check_confd_schema.py` 結束碼 2（`kind` 為 `multiple_documents`），`validate-config` 由 `yaml_syntax` 列 FAIL、結束碼 1。
