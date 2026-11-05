---
section: Changed
topic: da-tools
issues: [3611, 3577, 3609]
created: 2026-11-05T23:25:00+00:00
---
- **⚠️ Breaking：「沒能檢查」一律是 `2`，0/1/2 以外的結束碼要登記（[#3611](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3611)、[#3577](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3577)）**：引擎不在、輸入讀不到、resolver 逾時、`dc-run` 的容器或 docker 出狀況，原本各回 `3`／`4`／`1`／`127`，現在都是 `2`；真正的發現仍是 `1`。`_lib_exitcodes.EXTENDED_EXIT_CODES` 是允許其他結束碼的唯一清單，`run()` 會把未登記的結束碼改成 `2` 並印一行 ERROR。`dx_run --run` 與 `commit_helper` 照樣傳出被包裝命令的結束碼。逐格對照見 PR。
