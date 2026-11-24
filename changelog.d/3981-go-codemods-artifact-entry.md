---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-24T16:38:48+00:00
---
- **三支 Go codemod 改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`scripts/ops/add_t_parallel.py`、`route_metrics_through_field.py`、`use_get_metrics.py`。三支都先讀完所有檔、判斷完所有要改寫的目標才開始寫，被拒的目標不會讓前面的檔先被改掉；寫入失敗時是結束碼 `2` 加一行點名該檔與位置參數（`check the value given to FILE`），不再是 `unexpected internal error (PermissionError)`。`add_t_parallel` 遇到 FIFO（直接給的，或目錄裡名為 `*_test.go` 的）原本會卡住不返回、且先改寫了前面的檔，現在是結束碼 `2` 並且不寫任何檔。`route_metrics_through_field` 改成跟另兩支一樣只寫有改動的檔（原本每個檔都重寫，CRLF 會被改成 LF）。改寫一般以原子取代完成，權限是 git checkout 會給的（`0644`，原本有 owner 執行位元則 `0755`）；無法原子取代時退回就地寫入並印 `WARN`。
