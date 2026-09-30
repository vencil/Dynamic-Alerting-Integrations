---
section: Fixed
topic: exporter
issues: [2426]
created: 2026-09-29T14:07:37+00:00
---
- **`_state_maintenance` 的 `expires` 無法解析時，WARN 每次 scrape 只印一次（exporter、da-guard；[#2426](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2426)）**：樹裡宣告了 `state_filters.maintenance`（repo 附帶的 `conf.d/_defaults.yaml` 就有）時，同一次 scrape 會把 `WARN: invalid expires ... in _state_maintenance` 印兩次，`da-guard served-values` 也跟著印兩次。現在兩者都只印一次；未宣告 maintenance filter 的樹、合法的 `expires`、以及其他 WARN 的行為都不變。
