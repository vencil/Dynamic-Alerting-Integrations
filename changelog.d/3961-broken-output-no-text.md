---
section: Changed
topic: tools
issues: [3961]
created: 2026-11-15T09:00:00+00:00
---
- **da-guard 回覆形狀不合時，錯誤訊息不再帶出文件裡來自 conf.d 的文字（[#3961](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3961)）**：route generator、validate-config 等讀 served-values、key-verdict、effective、severity-dedup 的工具，遇到壞掉的輸出時那行錯誤只說位置與型別；值、鍵、租戶 id 經與 [#3703](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3703) 相同的顯示規則（名稱形照印，其餘說長度或印替身 `<key:…>`），不再以 Python repr 原樣印出。結束碼不變。產生器服務（tenant-api 的寫入判定）遇到 base 裡不是 UTF-8 的 conf.d 名字（任何 entry 類型、含目錄成分）時照 CLI 判定：da-guard 拒收整棵樹，名字只以 ADR-036 的跳脫形式點名一次，Python 端不再印出它。對 tenant-api：這種寫入不再以 rejected 拒絕（一般檔是可重試的 429 `git_read`，symlink／gitlink 是 `incomplete`），改為 422 findings（`run_failed`；base 判得完時 `PreExisting` 為 true），改名之前重試不會通過。寫入的 path 帶 lone surrogate（JSON 的 `\udcff`）時，服務原本編不出回應、連線無回應中斷，現在回 400（`path is not valid UTF-8`）；path 重複的 400 改以 index 指出，不印 path。
