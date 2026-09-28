---
section: Added
topic: ci
issues: [1471]
created: 2026-09-28T12:40:34+00:00
---
- **只改 benchmark 測試碼的 PR 改在當下量一次工作定義的效果（`bench-workload-record.yaml`；[#1471](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1471)）**：`bench-gate-pr` 與 `bench-attrib-main` 都排除 `*_test.go`，只改 bench fixture 的 PR 原本沒有任何 workflow 量它，工作定義的改變要到夜跑才以永久階梯現形。新 workflow 在 diff 碰到工作定義閉包（`.github/bench-reference.yaml` 的 `workload_closure`）、且沒有任何會觸發 `bench-gate-pr` 的檔案時，以同一套交錯量測比對 merge-base 與 PR head，結果只寫進 step summary 與 artifact：不留言、不貼 label，數字再差都不讓 job 失敗，只有量測腳本本身出錯才會紅；跳過時寫明原因。既有兩支閘門的行為不變。
