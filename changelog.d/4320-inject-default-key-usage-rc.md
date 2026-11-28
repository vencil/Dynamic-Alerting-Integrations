---
section: Changed
topic: da-tools
issues: [4320]
created: 2026-11-28T17:51:16+00:00
---
- **`inject_default_key` 的呼叫端錯誤改回 rc 2（⚠️ breaking；[#4320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4320)）**：參數數量不對、值不是數字、鍵不能放進 `defaults:`（`_critical` 後綴、維度鍵）這三種，原本回 rc 1，現在依 0／1／2 結束碼規則回 rc 2，同樣不寫出任何檔案。唯一的呼叫端 `bench_e2e_run.sh` 只看是否非零，不受影響。
