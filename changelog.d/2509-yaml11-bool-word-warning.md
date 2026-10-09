---
section: Added
topic: confd-reader-consistency
issues: [2509, 2740]
created: 2026-10-07T18:03:00+00:00
---
- **布林欄位寫了未加引號的 `yes` / `no` / `on` / `off` 時，conf.d lint 給 WARN（tools；[#2509](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2509)）**：`send_resolved: on`、`_routing_enforced.enabled: yes`、`_state_maintenance.enabled: off` 這類 YAML 1.1 字眼，PyYAML 與 schema 讀成布林，exporter 的部分 reader 會保留成字串（各檔種由不同的 reader 讀，結果不一）。`check_confd_schema`（confd-schema hook 改以 verbose 執行）與 `validate-config` 的 `yaml_quoting` 列會指名檔案、行號與欄位，建議改寫成 `true` / `false`；只是 WARN，結束碼不變。根 `_defaults*.yaml` 的 `tenants:` 區塊改依租戶 schema 檢查，同樣會報這個 WARN 與字串欄位未加引號的 ERROR。明確寫 `!!bool yes` 這類 yaml.v3 不接受的寫法，`check_confd_schema` 一律給 WARN，exporter 讀不讀得了這份檔由 `da-guard` 判斷：`validate-config` 的 `yaml_quoting` 列只為 da-guard 判定讀不了的檔保留這則 WARN（`profiles` 列連同理由報 FAIL），`_routing_profiles.yaml` 這類不受影響的檔不報；沒有 da-guard 時照舊全列（[#2740](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2740)）。`!!bool y`、`!!int x` 這類 PyYAML 建不出的值，`check_confd_schema` 改為具名報 ERROR（這份檔沒做 schema 檢查），不再丟 traceback。
