---
section: Fixed
topic: silent-maintenance
issues: [1988]
created: 2026-09-26T17:00:00+00:00
---
- **文件、portal 與範例的 silent mode 寫法改為 exporter 實際接受的（docs、portal）**：`tenant-lifecycle` 原本教的 `_state_silent_mode` 寫法會被 CLI 拒收、也不是 exporter 讀的 key，改教 `patch-config <tenant> _silent_mode '{target: all, expires: "<RFC3339>"}'`；各文件與 `demo-showcase.sh` 已過期的 `expires` 範例改為 2099，格式統一標為 RFC3339。portal playground 不再檢查 `_silent_mode`（原本把合法值判錯），Schema Explorer 改依 schema 顯示可接受的值，tenant-manager 產出的 YAML 改為每個租戶一段 `_silent_mode`（[#1988](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1988)）。
