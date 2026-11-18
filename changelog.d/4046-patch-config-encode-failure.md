---
section: Fixed
topic: da-tools
issues: [4046]
created: 2026-11-18T22:48:29+00:00
---
- **`patch-config --json`：ConfigMap 寫入後回應文件編碼失敗時，結束碼照實說明 ConfigMap 狀態（da-tools；[#4046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4046)）**：之前這種失敗以內部錯誤的 `2` 結束——那是「呼叫端錯誤、什麼都沒寫」的碼，而 ConfigMap 已經是新內容。現在與 stdout 寫不進去相同：stderr 印一行，結束碼保留 patch-config 已決定、說明 ConfigMap 實況的碼。注入矩陣裡測這個編碼器的列原本因名稱指錯模組而一律 skip，現在照常量測；名單裡的名稱不存在改為測試失敗。
