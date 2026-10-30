---
section: Changed
topic: da-tools
issues: [3206]
created: 2026-10-29T17:28:27+00:00
---
- **`check_threshold_reachability` 讀產生器寫出的 `_defaults.yaml` 改經 `da-guard parse-tree`（[#3206](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3206)，[ADR-036](docs/adr/036-single-parser-effective-config.md) §12）**：`da-tools init` 產生的 `_defaults.yaml` 以 exporter 的讀法（yaml.v3）讀，沒有 da-guard 就 rc 2，不退回 PyYAML。⚠️ 產生器寫出 exporter 整份拒收的 `_defaults.yaml` 時，視為 floor breach（`--ci` rc 1），不再當成空的鍵集合。
