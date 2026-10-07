---
section: Added
topic: confd-reader-consistency
issues: [2509]
created: 2026-10-07T18:03:00+00:00
---
- **布林欄位寫了未加引號的 `yes` / `no` / `on` / `off` 時，conf.d lint 給 WARN（tools；[#2509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2509)）**：`send_resolved: on`、`_routing_enforced.enabled: yes`、`_state_maintenance.enabled: off` 這類 YAML 1.1 字眼，PyYAML 與 schema 讀成布林，exporter 的 generic effective config（`da-guard effective`）與 merged_hash 卻讀到字串。`check_confd_schema`（pre-commit 的 confd-schema hook，現在以 verbose 執行才看得到通過時的輸出）與 `validate-config` 的 `yaml_quoting` 列會指名檔案、行號與欄位，建議改寫成 `true` / `false`；只是 WARN，結束碼不變。根 `_defaults*.yaml` 的 `tenants:` 區塊改依租戶 schema 檢查，同樣會報這個 WARN 與字串欄位未加引號的 ERROR。明確寫 `!!bool yes` 這類 tag 的寫法，exporter 會丟掉整份檔，因此報 ERROR。
