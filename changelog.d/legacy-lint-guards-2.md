---
section: Fixed
topic: lint-guards
issues: [1398, 1492, 1620, 1642, 1695, 1697, 1702, 1703, 1704, 1705, 1706, 1772, 1821, 1842, 1863, 1870, 1877, 1984, 1987, 2025]
created: 2026-09-26T17:00:00+00:00
---
- **沉默失效的守衛修活或退役（lint、dx）**：一批「看起來綠、其實沒檢查」的閘門被修正：`md-yaml-drift-check` 解除 manual 擱置並改驗正確的 schema；多支 hook 的 `files:` 仍指向 portal 搬遷前的舊路徑而從不觸發、兩支手動 hook 傳錯參數每次 exit 2，均已修正；diff-only lint 在 `git diff` 失敗或檔案被改名搬移時不再回報「沒有違規」；go-lint 改以探針實測各 module 的 linter 下限，不再能被 `default: fast`、`exclusions` 或 `settings` 整個 module 靜音；零讀者的 `check_roadmap_changelog_overlap` 與重複的 `flow-e2e-check` 退役。`validate_all`：⚠️ `--only`／`--skip` 收到不存在的檢查名稱改為 `exit 2`（原本靜默少跑一支，[#1620](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1620)）；`--skip` 會真的從 `--only` 扣除；⚠️ 互斥的旗標組合（含 `--watch` 搭其他旗標）改為 `exit 2`，`--smart` 判定無檢查可跑時改為不跑、`exit 0`；`--diff-report` 遇到有未 staged 改動的工作樹改為拒跑，以免 `git checkout .` 清掉改動（[#1706](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1706)）；`--json` 的 stdout 不再夾雜人類文字。
