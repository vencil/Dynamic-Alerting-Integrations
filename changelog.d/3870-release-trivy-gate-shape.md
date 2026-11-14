---
section: Security
topic: release
issues: [3870]
created: 2026-11-14T07:09:42+00:00
---
- **release 的 Trivy 掃描不能再被悄悄停用（release.yaml；[#3870](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3870)）**：守衛原本只檢查「先掃再發布」的順序，Trivy 步驟加 `if: false`、`continue-on-error: true` 或把 `exit-code` 改成 0 都不會轉紅。現在解析 YAML 檢查掃描能讓 job 失敗：步驟只准 `name`／`uses`／`with`、`exit-code` 字面 1、只准審過的輸入、`trivyignores` 只能是所掃映像自己的豁免檔、掃描後的步驟 `if:` 不得呼叫狀態函式、任何層級不得設 `TRIVY_*` env、repo 根目錄不得有 Trivy 預設會讀的 `trivy.yaml`／`trivy-secret.yaml`。`severity: CRITICAL,HIGH` 與 `ignore-unfixed: true` 收斂成 release、nightly、PR 三處掃描共用的單一來源，逐一檢查每個 workflow，且不准 `format: sarif`（trivy-action 會因此拿掉 severity 篩選）。
