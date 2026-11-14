---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-14T05:15:34+00:00
---
- **必要檢查不再依變更路徑跳過，每支 PR 都跑全部必要檢查（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：⚠️ 行為變更。`ci.yml`、`docs-ci.yaml`、`validate.yaml` 原本用 `dorny/paths-filter` 判斷變更路徑，沒命中的 job 會被跳過，並由 aggregate gate 判通過；filter 是手寫清單，漏列一個輸入時，該跑的檢查被跳過而必要檢查照樣綠。現在三支 workflow 的 `detect-changes` 與各 job 的 path 條件全部移除：Go／Python／Portal／Smoke 測試、Go Lint、租戶設定與路由驗證、版號一致性與文件檢查每支 PR 都執行，aggregate gate 只在它等待的每個 job 都是 `success` 時通過，被跳過或取消一律判失敗。必要檢查的名稱不變。非必要的 `I-4 Runbook Smoke Test` 也改為每支 PR 都跑。圍繞 path filter 的守衛（測試讀到的檔是否都在 filter 內、Go 測試執行期的讀檔比對 `go_test_reads.py`）隨之移除；新的守衛擋下被加回來的 job 層 path 條件、`dorny/paths-filter` 步驟與「跳過即通過」的 gate。
