---
section: Fixed
topic: exporter
issues: [4236]
created: 2026-11-22T13:22:15+00:00
---
- **da-guard `root_default_refused`：根 `defaults:` 同時寫一個門檻的兩種拼寫時，訊息照 exporter 實際讀的那一行說（da-guard；[#4236](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4236)）**：例如 `mysql_cpu: abc` 旁有 `mysql_threads_running: 7` 時，exporter 照送 7、忽略被拒的那行，訊息改說「刪掉這行」，不再說「不送預設列」；被拒的值反而蓋掉另一種拼寫時（`mysql_threads_running: "off"` 旁有 `mysql_cpu: 7`），訊息點出 7 沒送出。判斷直接問 exporter 的解碼，不另寫一份規則。severity 與結束碼不變。
