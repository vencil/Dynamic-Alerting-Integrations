---
section: Added
topic: confd-reader-consistency
issues: [2509]
created: 2026-10-07T18:03:00+00:00
---
- **布林欄位寫了未加引號的 `yes` / `no` / `on` / `off` 時，conf.d lint 給 WARN（tools；[#2509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2509)）**：`send_resolved: on`、`_routing_enforced.enabled: yes`、`_state_maintenance.enabled: off` 這類 YAML 1.1 字眼，PyYAML 與 schema 讀成布林，exporter（yaml.v3）卻讀成字串，兩邊看到的值與 merged_hash 都不同。`check_confd_schema`（pre-commit 的 confd-schema hook，現在以 verbose 執行才看得到通過時的輸出）與 `validate-config` 的 `yaml_quoting` 列會指名檔案、行號與欄位，建議改寫成 `true` / `false`。只是 WARN，結束碼不變，讀取端也不變；`true` / `false` 與加引號的寫法不受影響。
