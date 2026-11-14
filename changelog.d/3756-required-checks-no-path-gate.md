---
section: Changed
topic: ci
issues: [3756]
created: 2026-11-14T05:15:34+00:00
---
- **必要檢查不再依變更路徑跳過，每支 PR 都跑全部必要檢查（ci；[#3756](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3756)）**：⚠️ 行為變更。`ci.yml`、`docs-ci.yaml`、`validate.yaml` 原本用 `dorny/paths-filter` 判斷變更路徑，沒命中的 job 會被跳過，並由 aggregate gate 判通過；filter 是手寫清單，漏列一個輸入時，該跑的檢查被跳過而必要檢查照樣綠。現在三支 workflow 的 `detect-changes` 與各 job 的 path 條件全部移除：Go／Python／Portal／Smoke 測試、Go Lint、租戶設定與路由驗證、版號一致性與文件檢查每支 PR 都執行，aggregate gate 只在它等待的每個 job 都是 `success` 時通過，被跳過或取消一律判失敗。既有必要檢查的名稱不變；`Federation E2E (ADR-020 IV-2j)` 新增為必要檢查（登記為 pending，合入後由 owner 在設定啟用），它的 runner 先重試拉映像，拉不到時以 exit 2 結束並點名是拉映像失敗。非必要的 `I-4 Runbook Smoke Test` 也改為每支 PR 都跑。Smoke 測試不再對 PR 留言，改寫進該次執行的 step summary。圍繞 path filter 的守衛（測試讀到的檔是否都在 filter 內、Go 測試執行期的讀檔比對 `go_test_reads.py`）隨之移除；新的守衛擋下被加回來的 job 層 path 條件、path-filter action 步驟（名單一份，不分大小寫，`workflow_path_filter.py` 與契約測試共用）與「跳過即通過」的 gate；回報必要檢查的 workflow 裡，每個 job 都要落在某個登記於 `.github/required-checks.yaml`（required 或 advisory）的檢查的 `needs` 閉包內，gate 等待的 job 不得是 reusable workflow 呼叫。
