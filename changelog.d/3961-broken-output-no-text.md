---
section: Changed
topic: tools
issues: [3961]
created: 2026-11-15T09:00:00+00:00
---
- **da-guard 回覆形狀不合時，錯誤訊息不再帶出文件裡來自 conf.d 的文字（[#3961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3961)）**：route generator、validate-config 等讀 served-values、key-verdict、effective、severity-dedup 的工具，遇到壞掉的輸出時那行錯誤只說位置與型別；值、鍵、租戶 id 經與 [#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703) 相同的顯示規則（名稱形照印，其餘說長度或印替身 `<key:…>`），不再以 Python repr 原樣印出。結束碼不變。產生器服務（tenant-api 的寫入判定）遇到 commit 裡不是 UTF-8 的 conf.d 檔名，不再以 git 讀取失敗（`git_read`）拒收並在 `stderr_tail` 帶出整筆 ls-tree 紀錄，而是照 CLI 判定：da-guard 拒收整棵樹（rc 2），檔名只以 ADR-036 的跳脫形式點名一次。
