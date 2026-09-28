---
section: Changed
topic: da-tools
issues: [1550]
created: 2026-09-28T16:40:00+08:00
---
- **`describe_tenant` 回報的路徑在 Windows 上也一律用 `/`（da-tools；[#1550](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1550)）**：`--show-sources` 輸出的 `source_file` 與 `defaults_chain` 以前在 Windows 上是 `db\_defaults.yaml`，現在與 Linux、Go 端（exporter／tenant-api）一致，都是 `db/_defaults.yaml`；`tenant-verify` 轉印的也是同一組值。golden parity 測試因此不再需要在 Windows 上整批跳過。
