---
section: Changed
topic: tenant-api
issues: [1535]
created: 2026-10-17T19:57:00+00:00
---
- **state store 的 dir 模式寫明只供開發與 try-local，外部寫入者必須拿 flock（tenant-api；[#1535](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1535)）**：dir 模式的鎖是 advisory flock，只擋同樣去拿它的程序。外部腳本或手動修改沒有先對 `$TA_STATE_DIR/.lock` 取得 exclusive flock、涵蓋整段讀改寫，就會被 tenant-api 的下一次寫入靜默覆蓋（API 照樣回成功、log 無警告）；拿了同一把鎖則修改會保留，因為 tenant-api 在鎖內才重讀紀錄。行為不變，新增測試釘住這兩面。正式環境用 `configmap` 模式。規則與範例見 tenant-api README「dir 模式的外部寫入者必須拿 flock」。
