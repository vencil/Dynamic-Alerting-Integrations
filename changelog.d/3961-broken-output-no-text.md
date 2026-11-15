---
section: Changed
topic: tools
issues: [3961]
created: 2026-11-15T09:00:00+00:00
---
- **⚠️ BREAKING：產生器服務的回應移除 `message` 與 `stderr_tail`；da-guard 讀取端的錯誤不再帶出 conf.d 文字（[#3961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3961)）**：`POST /v1/validate` 只回封閉的 `error`／`reason` 與結構化欄位（503 `tool_fault` 另帶結構化的 `tool_fault`），細節只進服務自己的 log；回應以 ASCII 編碼，任何值都編得出來。tenant-api 須同版（它本來就由 reason 組出給呼叫端的文字）。讀 served-values、key-verdict、effective、severity-dedup、parse-tree 的工具遇到壞掉的輸出時，錯誤只說位置與型別，值、鍵、租戶 id 經 [#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703) 的顯示規則。base 裡有不是 UTF-8 的 conf.d 名字時，服務照 CLI 交 da-guard 判：tenant-api 的寫入由 rejected（429 `git_read`／`incomplete`）改為 422 findings（`run_failed`，base 判得完時 `PreExisting` 為 true），改名前重試不會通過。寫入 path 帶 lone surrogate 時回 400。
