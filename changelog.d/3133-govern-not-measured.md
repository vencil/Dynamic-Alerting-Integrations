---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-23T06:00:00+00:00
---
- **⚠️ 結束碼變更：threshold-govern 有閾值沒量成時，報告標為不完整、結束碼 2（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133)）**：Prometheus 查詢沒得到回答的閾值先前被靜默略過，Prometheus 整個連不上時印「No thresholds need governance」、結束碼 0。現在文字報告另列一節；`--json` 多 `not_measured`，頂層 `status` 為 `partial`；stderr 印一行 `ERROR`，結束碼 0 → 2（dry-run 也是，且優先於寫入失敗的 1）。量到的閾值照常治理、`--apply` 照常開 PR。
