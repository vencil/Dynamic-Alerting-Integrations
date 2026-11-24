---
section: Fixed
topic: ci
issues: [3981]
created: 2026-11-24T18:30:54+00:00
---
- **`check_includes_sync.py --fix` 改經共用的寫入入口（[#3981](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3981)）**：`docs/includes/` 裡名字像 include 的 FIFO，原本會讓 `--check` 與 `--fix` 卡住不返回（`--fix` 還會先寫出前面的 stub）；include 讀不到或不是 UTF-8 時原本是 `unexpected internal error`。現在兩者都是結束碼 `2` 加一行點名該檔，`--fix` 也不寫任何 stub。非一般檔一律擋下：指向 `/dev/null` 這類裝置的 include 原本當成空檔（結束碼 `0`），現在也是結束碼 `2`。所有 stub 都算完才開始寫；寫入失敗時是結束碼 `2` 加一行點名該 stub。stub 以原子取代寫出，權限 `0644`（與原本相同）。stub 的「建立」訊息（文字不變）改在寫完之後、摘要之前印出，不再穿插在比對結果之間。
