---
section: Fixed
topic: alertmanager-routing
issues: [2906]
created: 2026-10-17T12:02:00+00:00
---
- **產生器容器的判讀在記憶體上限下被殺掉的 da-guard 算成 `rejected`／`memory`（generator-service；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：判讀行程設了 RLIMIT_DATA 時，它啟動的 da-guard 帶 `GOTRACEBACK=crash`（RLIMIT_CORE 設 0，不留 core 檔），Go runtime 不論是記憶體不足、runtime 內部的 SIGSEGV 或建不了執行緒，一律以訊號（SIGABRT）結束；被訊號結束即算這筆寫入超出記憶體上限，訊息帶訊號名稱。判斷依據是行程狀態，不比對錯誤文字（先前比對 `out of memory` 字樣，會漏掉 SIGSEGV 與 `pthread_create failed`，那時回成 rc 2 的 findings）。沒設上限的呼叫（CLI）被訊號結束照舊是 da-guard 失敗、結束碼 2。快取只存「與鍵同一份 bytes」的解析結果：裝了快取時每次 miss 都以單檔模式解析算鍵用的那份 bytes。
