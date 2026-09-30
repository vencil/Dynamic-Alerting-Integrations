---
section: Fixed
topic: ci
issues: [1518]
created: 2026-09-29T23:25:24+00:00
---
- **PR 報告留言不再被較晚跑完的舊 commit 蓋掉（ci；[#1518](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1518)）**：Blast Radius、Config Diff、Dangling Defaults Guard 三支 workflow 都會原地改寫同一則 sticky 留言，先前沒有 concurrency，同一個 PR 連推兩次時由較晚跑完的那一輪勝出，可能是舊 commit。現在三支都以「workflow + PR 號」為一組，同一個 PR 的新 push 會取消舊的 run；手動 dispatch 等非 PR 事件各自獨立成組，不取消別人、也不被取消。三則留言原本就帶 head SHA 與 run 連結，新增測試實際執行留言腳本來守住這一點。
