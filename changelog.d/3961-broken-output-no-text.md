---
section: Changed
topic: tools
issues: [3961]
created: 2026-11-15T09:00:00+00:00
---
- **da-guard 回覆形狀不合時，錯誤訊息不再帶出文件裡來自 conf.d 的文字（[#3961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3961)）**：route generator、validate-config 等讀 served-values、key-verdict、effective、severity-dedup 的工具，遇到壞掉的輸出時那行錯誤只說位置與型別；值、鍵、租戶 id 經與 [#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703) 相同的顯示規則（名稱形照印，其餘說長度或印替身 `<key:…>`），不再以 Python repr 原樣印出。結束碼不變。
