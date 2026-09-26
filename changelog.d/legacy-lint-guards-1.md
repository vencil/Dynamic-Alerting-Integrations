---
section: Added
topic: lint-guards
issues: [1185, 1280, 1353, 1398, 1492, 1699, 1736, 1751, 1816, 1821, 1845, 1984, 1992]
created: 2026-09-26T17:00:00+00:00
---
- **Lint 與 pre-commit 守衛擴充（lint）**：多個原本靠 review 抓的缺陷類別改由 commit／CI 機械擋下：`actionlint` 檢查全部 GitHub workflow（含 untrusted input 直接拼進 `run:` 的 script injection）；五份 `.golangci.yml` 補上 `formatters:`（`gofmt`）、tenant-api 與 threshold-exporter 啟用 `godoclint`，golangci-lint 受檢範圍擴到全部 Go module 與 build-tag 檔；`docs/` 內每個 yaml 圍欄必須可解析、內嵌的 k8s／CRD 物件對釘版 CRD schema 驗證（`md-yaml-fence-check`／`md-yaml-crd-check`）。另新增 JSX ARIA 參照檢查、errexit＋pipefail 下讀 `PIPESTATUS` 的檢查、`verify-diff-check`、偵測「存在卻沒有任何 runner 執行」的 dead lint（`check_orphan_lint.py`），以及用真的 `pre-commit run` 驗 hook 設定沒被改壞的存活測試。⚠️ `open-encoding-audit` 由 warn-only 改為阻擋（[#1984](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1984)）。
