---
section: Added
topic: release-versioning
issues: [1269, 1352, 1532, 1597, 1755]
created: 2026-09-26T17:00:00+00:00
---
- **Release 產物閘門（internal、ci、helm）**：新增 image-pin 能力檢查（pin 到的 da-tools image 是否真的含有 workload 要跑的子命令）、chart 出貨面與 `.tgz` 內容檢查、「教客戶拉的 chart 必須真的有 `helm push`」檢查，以及 `make pre-tag` 遇到未發布的 draft security advisory 時擋下（`ADVISORY_ACK=1` 明示略過）（[#1755](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1755)、[#1352](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1352)）。da-portal chart 開始隨 portal 線發布（首次 push 建立的 GHCR package 預設 private，需 owner 手動設為 public 才能匿名 `helm install`）；recipe-preview 線改標為「已設計、尚未啟用」。⚠️ 現存的 `cronjob-threshold-govern` 與 `federation-reconciler` 釘在 `da-tools:v2.9.0`，該 image 不含它們要跑的程式，需下一次 `tools/v*` release 升 pin。
