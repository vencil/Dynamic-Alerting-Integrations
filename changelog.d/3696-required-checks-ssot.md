---
section: Changed
topic: ci
issues: [3696]
created: 2026-11-10T14:59:34+00:00
---
- **main 的必要檢查清單改以 `.github/required-checks.yaml` 為唯一來源（ci；[#3696](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3696)）**：每支 PR 由 `tests/ops/test_required_checks_ssot.py` 檢查清單裡每個名稱都由一個一定會回報的 job 回報——同名唯一、所在 workflow 的 `pull_request` 不帶 `paths`，不經 aggregate gate 的 job 不得有 `needs`／`if`／matrix。新的 `required-checks-drift.yml` 每日拿 GitHub 實際設定比對這個檔，排程失敗開票；它本身不擋 merge。刻意不擋 merge 的檢查附理由列在同檔的 `advisory:`，workflow 註解裡「某某不是必要檢查（某日量測）」的散文改為指向該檔，並由測試要求日後同類說法都要引用它。新增或改名必要檢查的順序寫在檔頭。
