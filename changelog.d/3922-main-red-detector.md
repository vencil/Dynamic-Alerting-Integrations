---
section: Added
topic: ci
issues: [3922]
created: 2026-11-14T19:44:45+00:00
---
- **main 上必要檢查失敗時自動開票、恢復時自動關票（ci；[#3922](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3922)）**：新的 `main-red-detector.yml` 在回報必要檢查的 workflow 於 main 的 push run 結束時執行，每次都重讀每一支被監看 workflow 在 main 上最新一筆有結論的 run 再決定——有失敗而沒有票就開一張固定標題的票，寫明範圍「該 workflow 上一次成功的 sha .. 失敗的 sha」、失敗的 job、run 連結與範圍內的 commit；票開著時只在出現新的失敗 run、或某支由失敗轉綠時留言；全部成功時留言並關票。`cancelled` 不算失敗也不算恢復。追蹤票只認 `github-actions[bot]` 開的，別人開的同標題票不會被留言或關閉。判斷在 `scripts/ops/main_red_report.py`：結束碼 0 無事、1 main 是紅的或票仍開著、2 沒結論——查票或查 run 失敗時不開票也不關票。被監看的清單由測試釘在 `.github/required-checks.yaml` 推導出的集合上。`validate.yaml` 補上 push to main 觸發，它回報的必要檢查從此也在合併後的 main 上跑。⚠️ `workflow_run` 只從預設分支觸發，合入前無法在 GitHub 上實跑；合入後可用 `workflow_dispatch` 帶 run id 重放（預設不寫票）。
