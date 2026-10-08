---
section: Added
topic: confd-reader-consistency
issues: [2509]
created: 2026-10-07T18:03:00+00:00
---
- **布林欄位寫了未加引號的 `yes` / `no` / `on` / `off` 時，conf.d lint 給 WARN（tools；[#2509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2509)）**：`send_resolved: on`、`_routing_enforced.enabled: yes`、`_state_maintenance.enabled: off` 這類 YAML 1.1 字眼，PyYAML 與 schema 讀成布林，exporter 的部分 reader 會保留成字串（各檔種由不同的 reader 讀，結果不一）。`check_confd_schema`（pre-commit 的 confd-schema hook，現在以 verbose 執行才看得到通過時的輸出）與 `validate-config` 的 `yaml_quoting` 列會指名檔案、行號與欄位，建議改寫成 `true` / `false`；只是 WARN，結束碼不變。根 `_defaults*.yaml` 的 `tenants:` 區塊改依租戶 schema 檢查，同樣會報這個 WARN 與字串欄位未加引號的 ERROR。明確寫 `!!bool yes` 這類 yaml.v3 不收的 tag：只有在實測會讓 exporter 整檔 decode 失敗（`da-guard` exit 3、列入 `parse_failed`）的位置報 ERROR——租戶檔的第一份文件，以及根 `_defaults*.yaml` 的 `defaults:`／`tenants:`；其他位置（`_routing_profiles.yaml`、第二份以後的文件等）報 WARN。`!!bool y` 這類 PyYAML 也建不出的值，`check_confd_schema` 改為具名報 ERROR，不再丟 traceback。
